"""Deterministic return capture and notification-driven runtime transport."""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Protocol

from multiagent import measure_twice as measure, review_workflow as review

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SynthesisJudgment:
    verdict: str
    minor_only: bool = False


@dataclass(frozen=True, slots=True)
class Completion:
    handle: str
    content: bytes
    status: str = "ok"
    judgment: SynthesisJudgment | None = None
    diagnostic: str | None = None


class NativeRuntime(Protocol):
    def launch(self, item: measure.MeasureWorkItem) -> str: ...
    def completions(self, handles: tuple[str, ...]) -> Iterable[Completion]: ...


class CancellableRuntime(NativeRuntime, Protocol):
    def cancel(self, handle: str) -> bool: ...


def render_argv(argv: Iterable[str]) -> str:
    return " ".join(review.quote_argv(word) for word in argv)


def write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(content)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def capture_review_return(ref: review.ReviewRef, action_id: str, content: bytes, *,
                          status: str = "ok", judgment: SynthesisJudgment | None = None,
                          diagnostic: str | None = None, capture_path: str | None = None) -> review.ReviewStep:
    run = review._guard_review_path(session_segment=ref.session_segment, run_id=ref.run_id, create=False)
    with review._owned_workflow_lock(run, ref):
        wf, run = review._load_current_locked(ref, run)
        action = review._load_action(wf, action_id)
        if review._action_attempt(action) != ref.attempt_id:
            raise review.WorkflowError("stale_ref", "capture belongs to another attempt", "stale_ref")
        if action["driver"] == "external" or action["submission_path"] is None:
            raise review.WorkflowError("invalid_action", "capture requires an issued non-external action")
        issued_paths = review._issued_artifact_paths(action)
        path_value = capture_path or (action["return_transport"]["fallback"]["ingress_path"]
            if action["kind"] == "reviewer" else action["ingress_path"])
        if path_value not in issued_paths:
            raise review.WorkflowError("invalid_submission", "capture path was not issued")
        ingress = review._prepare_run_descendant(run, Path(path_value), "return ingress", create_parents=False)
        artifact = {"path": str(ingress), "sha256": measure.sha256(content)} if status == "ok" else None
        result = review.HostResult(ref, action_id, status, artifact,
                                   dataclasses.asdict(judgment) if judgment else None, diagnostic)
        result = review.parse_host_result(review.host_result_to_dict(result))
        # The submission guard validates the exact observation before any write;
        # malformed reviewer output still settles through its normal failure path.
        review._validate_submission_locked(wf, action, result, run, observed_bytes=content)
        # Canonical envelope bytes are stable across consumed-ingress replays.
        body = measure._canonical(review.host_result_to_dict(result))
        receipt = run / "attempts" / ref.attempt_id / "transport-receipts" / f"{review._hash_action(action_id)}.json"
        receipt_body = measure._canonical({"result": review.host_result_to_dict(result),
            "returned_sha256": measure.sha256(content), "capture_path": path_value})
        if receipt.parent.exists():
            receipt = review._prepare_run_descendant(run, receipt, "capture receipt", create_parents=False)
        if receipt.exists():
            retained_body = receipt.read_bytes()
            try:
                record = json.loads(retained_body)
                # Older successful receipts already bind the artifact path;
                # older failures used the default transport without an ingress.
                if isinstance(record, dict) and set(record) == {"result", "returned_sha256"}:
                    retained_result = review.parse_host_result(record["result"])
                    record["capture_path"] = (retained_result.artifact["path"] if retained_result.artifact else
                        action["return_transport"]["fallback"]["ingress_path"] if action["kind"] == "reviewer" else action["ingress_path"])
            except (ValueError, TypeError) as exc:
                raise review.WorkflowError("invalid_submission", f"capture receipt is unreadable: {exc}") from exc
            if record != json.loads(receipt_body):
                raise review.WorkflowError("conflict", "capture differs from the durable transport receipt", "conflict")
            receipt_body = retained_body  # Historical receipts stay immutable.
        if action["status"] == "settled":
            if (not receipt.is_file() or receipt.read_bytes() != receipt_body
                    or action.get("submission_sha256") != measure.sha256(body)):
                raise review.WorkflowError("conflict", "capture differs from the durable accepted receipt", "conflict")
            return review._advance_locked(wf, run)
        if action["status"] != "claimed":
            raise review.WorkflowError("invalid_action", "capture requires a claimed action")
        submission = review._prepare_run_descendant(run, Path(action["submission_path"]), "submission envelope", create_parents=False)
        if ((ingress.exists() and ingress.read_bytes() != content) or
                (submission.exists() and submission.read_bytes() != body)):
            raise review.WorkflowError("conflict", "capture differs from retained ingress or envelope", "conflict")
        receipt = review._prepare_run_descendant(run, receipt, "capture receipt", create_parents=True)
        write_bytes(ingress, content)
        write_bytes(submission, body)
        measure._write_once(receipt, receipt_body, run)
    return review.submit_review(review.SubmissionRequest(str(submission), True, result))


def capture_measure_return(ref: measure.MeasureRef, action_id: str, content: bytes, *,
                           status: str = "ok", diagnostic: str | None = None,
                           capture_path: str | None = None) -> measure.MeasureStep:
    if diagnostic is not None and not isinstance(diagnostic, str):
        raise review.WorkflowError("invalid_submission", "advisor capture diagnostic must be text or null")
    result_holder: list[measure.MeasureResult] = []
    def prepare(data: dict[str, object], journal: measure.MeasureJournal) -> None:
        root = measure._namespace(ref, journal)
        primary = str(root / "ingress" / f"{action_id}.txt")
        fallback = str(root / "host-return" / f"{action_id}.txt")
        if capture_path is not None and capture_path not in {primary, fallback}:
            raise review.WorkflowError("invalid_submission", "advisor capture path was not issued")
        accepted = next((result for result in journal.accepted_actions if result.action_id == action_id), None)
        returned_sha = measure.sha256(content)
        receipt = measure._safe_path(root / "transport-receipts" / f"{action_id}.json", root)
        capture = {"status": status, "diagnostic": diagnostic, "returned_sha256": returned_sha,
                   "capture_path": capture_path or fallback}
        retained = None
        if receipt.exists():
            try:
                record = json.loads(receipt.read_bytes())
            except (OSError, ValueError) as exc:
                raise review.WorkflowError("invalid_submission", f"advisor capture receipt is unreadable: {exc}") from exc
            saved_capture = record.get("capture") if isinstance(record, dict) else None
            if isinstance(saved_capture, dict) and set(saved_capture) == {"status", "diagnostic", "returned_sha256"}:
                primary_path, fallback_path = measure._safe_path(Path(primary), root), measure._safe_path(Path(fallback), root)
                saved_capture = {**saved_capture, "capture_path": primary if primary_path.is_file() and not fallback_path.exists() else fallback}
            if not isinstance(record, dict) or set(record) != {"capture", "result"} or saved_capture != capture:
                raise review.WorkflowError("conflict", "advisor capture differs from durable transport receipt", "conflict")
            retained = measure.parse_measure_result(record["result"])
            if retained.ref != ref or retained.action_id != action_id or retained.returned_sha256 != returned_sha:
                raise review.WorkflowError("invalid_submission", "advisor capture receipt belongs to another action")
        if accepted:
            if ((retained is not None and accepted != retained) or (retained is None and
                    (accepted.returned_sha256 != returned_sha or accepted.status != status or accepted.diagnostic != diagnostic))):
                raise review.WorkflowError("conflict", "advisor return differs from accepted receipt", "conflict")
            result_holder.append(accepted)
            return
        action = journal.action
        if action is None or action.item.action_id != action_id or action.status not in {"claimed", "promotion_pending"}:
            raise review.WorkflowError("invalid_action", "advisor capture requires its claimed action")
        ingress = measure._safe_path(Path(capture_path or fallback), root)
        if ingress.exists() and ingress.read_bytes() != content:
            raise review.WorkflowError("conflict", "advisor ingress differs from the observed return", "conflict")
        plan = None
        if action.status == "promotion_pending":
            result = action.result
            if ((retained is not None and result != retained) or (retained is None and
                    (result.returned_sha256 != returned_sha or result.status != status or result.diagnostic != diagnostic))):
                raise review.WorkflowError("conflict", "advisor capture differs from sealed receipt", "conflict")
        elif retained is not None:
            result = retained
        else:
            digest = None
            result_status, result_diagnostic = status, diagnostic
            if status == "ok":
                path = measure._safe_path(Path(action.item.staging_path), measure._namespace(ref, journal))
                try:
                    plan = path.read_bytes()
                    text = plan.decode("utf-8")
                except (OSError, UnicodeDecodeError) as exc:
                    result_status = "failed"
                    result_diagnostic = f"Completed advisor's issued plan is unreadable: {exc}"
                else:
                    if text.strip():
                        digest = measure.sha256(plan)
                    else:
                        result_status = "failed"
                        result_diagnostic = "Completed advisor's issued plan is empty"
            result = measure.MeasureResult(ref, action_id, result_status, digest, returned_sha, result_diagnostic)
        result = measure.parse_measure_result(measure.result_to_dict(result))
        submission = measure._safe_path(Path(action.item.submission_path), root)
        body = measure._canonical(measure.result_to_dict(result))
        if submission.exists() and submission.read_bytes() != body:
            raise review.WorkflowError("conflict", "advisor envelope differs from the observed return", "conflict")
        if result.status == "ok":
            if plan is None:
                try:
                    plan = measure._sealed_plan_path(result, journal).read_bytes()
                except OSError as exc:
                    raise review.WorkflowError("plan_source_unreadable", f"captured advisor plan is not retained: {exc}") from exc
            # Retain the observation before its immutable successful receipt.
            measure._seal_advisor_plan(result, journal, plan)
        write_bytes(ingress, content)
        # The observed completion and its derived plan failure are immutable on
        # replay, even when somebody subsequently repairs the staging file.
        if retained is None:
            measure._write_once(receipt, measure._canonical({"capture": capture,
                "result": measure.result_to_dict(result)}), root)
        write_bytes(submission, body)
        result_holder.append(result)
    measure._transaction(ref, prepare)
    step = measure.submit_measure_action(measure.MeasureSubmission(result_holder[0]))
    # Submission replay relies on the LoopState receipt, never this disposable envelope.
    data = measure.loop_state.read(measure.loop_state.resolve("mt", ref.session_segment))
    journal = measure.journal_from_dict(data["mt_workflow"])
    root = measure._namespace(ref, journal)
    (root / "submissions" / f"{action_id}.json").unlink(missing_ok=True)
    return step


def run_measure_batch(step: measure.MeasureStep, runtime: NativeRuntime) -> measure.MeasureStep:
    if step.type != review.StepType.WORK_BATCH or step.ref is None:
        return step
    handles: dict[str, measure.MeasureWorkItem] = {}
    try:
        for item in step.work_items:
            if item.owner != step.ref:
                raise review.WorkflowError("invalid_runtime", "runtime work belongs to another loop")
            if item.review_item:
                if item.driver == "external":
                    raise review.WorkflowError("invalid_runtime", "external review execution uses its existing provider route")
                claim = review.claim_review_action(review.ClaimRequest(item.review_ref, item.action_id))
                admitted = claim.authorization in {"spawn", "perform"}
                authoritative = dataclasses.replace(item, review_item=claim.work_item) if admitted else item
                if admitted and (claim.work_item != item.review_item or
                        any(getattr(item, key) != getattr(claim.work_item, key)
                            for key in ("kind", "driver", "role", "model", "channel", "access", "prompt_path", "submission_path", "return_transport"))
                        or item.returned_path != claim.work_item.ingress_path):
                    raise review.WorkflowError("invalid_runtime", "runtime batch differs from the issued claim")
            else:
                claim = measure.claim_measure_action(step.ref, item.action_id)
                admitted = claim["authorization"] == "spawn"
                authoritative = measure.parse_measure_work(claim["work_item"]) if admitted else item
                if admitted and authoritative != item:
                    raise review.WorkflowError("invalid_runtime", "runtime batch differs from the issued claim")
            if admitted:
                handle = runtime.launch(authoritative)
                if handle in handles:
                    raise review.WorkflowError("invalid_runtime", "runtime returned a duplicate handle")
                handles[handle] = authoritative
        # Every independent item has launched before the first notification is awaited.
        for completion in runtime.completions(tuple(handles)):
            item = handles.pop(completion.handle, None)
            if item is None:
                raise review.WorkflowError("invalid_runtime", "completion handle was not issued")
            if item.review_item:
                capture_review_return(item.review_ref, item.action_id, completion.content, status=completion.status,
                                      judgment=completion.judgment, diagnostic=completion.diagnostic)
            else:
                capture_measure_return(step.ref, item.action_id, completion.content, status=completion.status,
                                       diagnostic=completion.diagnostic)
    except BaseException:
        cancel = getattr(runtime, "cancel", None)
        if handles and callable(cancel):
            for handle in handles:
                try:
                    if not cancel(handle):
                        logger.warning("Runtime could not stop owned handle %s", handle)
                except Exception as cancel_error:
                    logger.warning("Runtime cancellation failed for %s: %s", handle, cancel_error)
        elif handles:
            logger.warning("Native cancellation API unavailable; %s owned handles may still return", len(handles))
        raise
    return measure.next_measure_twice(step.ref)
