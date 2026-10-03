"""Opt-in capture of an observed Claude Code native completion, without recopying."""
from __future__ import annotations

import dataclasses
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from multiagent import channels, measure_twice as measure, review_workflow as review
from multiagent import workflow_transport as transport
from multiagent import build_workflow as build

T = TypeVar("T")
Ref = measure.MeasureRef | review.ReviewRef | build.BuildRef
SUPPORTED_VERSION = "2.1.287"
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class NativeLaunch:
    ref: Ref
    action_id: str
    handle: str
    output_file: Path


@dataclass(frozen=True, slots=True)
class NativeContext:
    root: Path
    prompt: Path
    claimed: bool
    accepted: bool


def launch_prompt(prompt: Path) -> str:
    return f"Read {prompt} and perform exactly the issued action."


def _with_context(launch: NativeLaunch, operation: Callable[[NativeContext], T]) -> T:
    if channels.current_host() != "claude":
        raise review.WorkflowError("unsupported_native_capture", "direct artifact capture requires Claude Code")
    if isinstance(launch.ref, build.BuildRef):
        def owned_build(_data: dict, journal: build.BuildJournal) -> T:
            if journal.executor.role != "crew:executor":
                raise review.WorkflowError("unsupported_native_capture", "capture requires the issued native executor")
            root = build._root(launch.ref)
            accepted = any(r.action_id == launch.action_id for r in journal.accepted_actions)
            action = journal.action
            claimed = action is not None and action.action_id == launch.action_id and action.status == "claimed"
            if not accepted and not claimed:
                raise review.WorkflowError("invalid_action", "native return has no owned claim or accepted receipt")
            if claimed and action.handle is not None and action.handle != launch.handle:
                raise review.WorkflowError("conflict", "native handle differs from its bound writer")
            prompt = root / "prompts" / f"{launch.action_id}.txt"
            result = operation(NativeContext(build._safe(root / "native-transport" / "anchor", launch.ref).parent, prompt, claimed, accepted))
            if claimed and action.handle is None:
                journal.action = dataclasses.replace(action, handle=launch.handle)
            return result
        return build._transaction(launch.ref, owned_build)
    if isinstance(launch.ref, review.ReviewRef):
        run = review._guard_review_path(session_segment=launch.ref.session_segment, run_id=launch.ref.run_id, create=False)
        with review._owned_workflow_lock(run, launch.ref):
            wf, run = review._load_current_locked(launch.ref, run)
            action = review._load_action(wf, launch.action_id)
            if action["driver"] != "native" or action["kind"] != "reviewer" or action.get("role") != "crew:reviewer":
                raise review.WorkflowError("unsupported_native_capture", "only issued native reviewer returns are supported")
            receipt = run / "attempts" / launch.ref.attempt_id / "native-transport" / f"{measure.sha256(launch.action_id.encode())}.launch.json"
            root = review._prepare_run_descendant(run, receipt, "native transport", create_parents=True).parent
            context = NativeContext(root, Path(action["prompt_path"]), action["status"] == "claimed", action["status"] == "settled")
            return operation(context)

    def owned(data: dict[str, object], journal: measure.MeasureJournal) -> T:
        root = measure._namespace(launch.ref, journal)
        action = journal.action
        accepted = any(result.action_id == launch.action_id for result in journal.accepted_actions)
        if action and action.item.action_id == launch.action_id:
            if action.item.driver != "native" or action.item.role != "crew:advisor":
                raise review.WorkflowError("unsupported_native_capture", "only the issued native advisor is supported")
            prompt = Path(action.item.prompt_path)
            claimed = action.status in {"claimed", "promotion_pending"}
        elif accepted and measure.ACTION_RE.fullmatch(launch.action_id):
            prompt, claimed = root / "prompts" / f"{launch.action_id}.txt", False
        else:
            raise review.WorkflowError("invalid_action", "native return is not an owned advisor action")
        return operation(NativeContext(measure._safe_path(root / "native-transport", root), prompt, claimed, accepted))
    return measure._transaction(launch.ref, owned)


def _host_transcript(launch: NativeLaunch) -> Path:
    return (Path.home().resolve() / ".claude" / "projects" / launch.output_file.parent.parent.parent.name
            / launch.ref.session_segment / "subagents" / f"agent-{launch.handle}.jsonl")


def _source_path(launch: NativeLaunch, *, retained: Path | None = None) -> Path:
    output = launch.output_file
    if (not isinstance(launch.handle, str) or re.fullmatch(r"a[0-9a-f]{6,64}", launch.handle) is None
            or not output.is_absolute() or output.name != f"{launch.handle}.output"
            or output.parent.name != "tasks" or output.parent.parent.name != launch.ref.session_segment):
        raise review.WorkflowError("invalid_native_binding", "output path does not name the observed handle/session")
    host_root = Path("/tmp").resolve() / f"claude-{os.getuid()}"
    if host_root not in output.parent.resolve().parents or any(path.is_symlink() for path in output.parents):
        raise review.WorkflowError("invalid_native_binding", "native output must be in its owned host task directory")
    if output.is_symlink():
        expected = _host_transcript(launch)
        if output.resolve() != expected or any(path.is_symlink() for path in (expected, *expected.parents)):
            raise review.WorkflowError("invalid_native_binding", "native alias must target its exact host project/session/handle transcript")
        return expected
    if retained is not None and not output.exists():
        if retained not in {output, _host_transcript(launch)}:
            raise review.WorkflowError("invalid_native_binding", "retained source is not the owned host artifact")
        return retained
    return output


def _paths(launch: NativeLaunch, context: NativeContext) -> tuple[Path, Path, bytes]:
    context.root.mkdir(parents=True, exist_ok=True)
    stem = measure.sha256(launch.action_id.encode())
    receipt = measure._safe_path(context.root / f"{stem}.launch.json", context.root)
    returned = measure._safe_path(context.root / f"{stem}.return.txt", context.root)
    retained = None
    if context.accepted and receipt.is_file():
        try:
            binding = json.loads(receipt.read_bytes())
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise review.WorkflowError("invalid_native_binding", "native launch receipt is unreadable") from exc
        if not isinstance(binding, dict) or not isinstance(binding.get("source_file"), str):
            raise review.WorkflowError("invalid_native_binding", "native launch receipt has no bound source")
        retained = Path(binding["source_file"])
    source = _source_path(launch, retained=retained)
    reference = (build.ref_to_dict(launch.ref) if isinstance(launch.ref, build.BuildRef) else
                 measure.ref_to_dict(launch.ref) if isinstance(launch.ref, measure.MeasureRef)
                 else review.review_ref_to_dict(launch.ref))
    body = measure._canonical({"schema": 1, "ref": reference, "action_id": launch.action_id,
        "handle": launch.handle, "output_file": str(launch.output_file), "source_file": str(source), "prompt_path": str(context.prompt),
        "prompt_sha256": measure.sha256(context.prompt.read_bytes()), "launch_prompt": launch_prompt(context.prompt)})
    return receipt, returned, body


def bind_native_launch(launch: NativeLaunch) -> dict[str, object]:
    """Bind metadata from the actual Agent launch. Do not read its running output."""
    def bind(context: NativeContext) -> dict[str, object]:
        receipt, _, body = _paths(launch, context)
        if receipt.exists():
            if receipt.read_bytes() != body:
                raise review.WorkflowError("conflict", "native launch differs from the owned binding", "conflict")
        else:
            if not context.claimed:
                raise review.WorkflowError("invalid_action", "bind requires an already claimed native action")
            transport.write_bytes(receipt, body)
        return {"bound": True, "action_id": launch.action_id, "handle": launch.handle}
    return _with_context(launch, bind)


def read_completed_return(launch: NativeLaunch, prompt: Path, *, completion_observed: bool) -> bytes:
    """Read exact message bytes only after the caller observed actual completion."""
    if completion_observed is not True:
        raise review.WorkflowError("completion_not_observed", "a hand-back or completion notification is required; file state is insufficient")
    try:
        raw = _source_path(launch).read_bytes()
        if len(raw) > MAX_ARTIFACT_BYTES or not raw.endswith(b"\n"):
            raise review.WorkflowError("unsupported_native_artifact", "native JSONL artifact is oversized or incompletely framed")
        records = [json.loads(line) for line in raw.decode("utf-8").split("\n")[:-1]]
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise review.WorkflowError("unsupported_native_artifact", f"native artifact is not readable complete UTF-8 JSONL: {exc}") from exc
    if not records or not all(isinstance(record, dict) for record in records):
        raise review.WorkflowError("unsupported_native_artifact", "native artifact records must be objects")
    reports: list[str] = []
    for record in records:
        if (record.get("agentId") != launch.handle or record.get("sessionId") != launch.ref.session_segment
                or record.get("cwd") != str(measure.loop_state.crew_base())
                or record.get("version") != SUPPORTED_VERSION):
            raise review.WorkflowError("invalid_native_binding", "native artifact has a foreign handle, owner, project or unsupported CLI version")
        if record.get("type") not in {"user", "assistant", "attachment"}:
            raise review.WorkflowError("unsupported_native_artifact", "native artifact record shape is unsupported")
        message = record.get("message")
        if record.get("type") == "assistant" and isinstance(message, dict):
            content = message.get("content")
            if not isinstance(content, list):
                raise review.WorkflowError("unsupported_native_artifact", "native assistant content must be a block list")
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "SubagentHandback":
                    inputs = block.get("input")
                    if not isinstance(inputs, dict) or set(inputs) != {"message"} or not isinstance(inputs["message"], str):
                        raise review.WorkflowError("unsupported_native_artifact", "native hand-back must contain one exact message string")
                    reports.append(inputs["message"])
    first = records[0]
    if (first.get("type") != "user" or first.get("message") != {"role": "user", "content": launch_prompt(prompt)}):
        raise review.WorkflowError("invalid_native_binding", "native artifact was launched with a foreign or ambiguous prompt")
    if len(reports) != 1 or not reports[0].strip():
        raise review.WorkflowError("unsupported_native_artifact", "native artifact needs exactly one nonblank hand-back; it cannot establish completion")
    try:
        return reports[0].encode("utf-8")
    except UnicodeError as exc:
        raise review.WorkflowError("unsupported_native_artifact", "native message is not valid UTF-8") from exc


def capture_native_return(launch: NativeLaunch, *, completion_observed: bool,
                          status: str = "ok", diagnostic: str | None = None) -> measure.MeasureStep | review.ReviewStep | build.BuildStep:
    if completion_observed is not True:
        raise review.WorkflowError("completion_not_observed", "actual owned completion must be observed before capture")
    def capture(context: NativeContext) -> bytes:
        receipt, returned, body = _paths(launch, context)
        if not receipt.is_file() or receipt.read_bytes() != body:
            raise review.WorkflowError("invalid_native_binding", "native capture requires its exact owned launch binding")
        if context.accepted:
            return returned.read_bytes()
        if not context.claimed:
            raise review.WorkflowError("invalid_action", "native capture requires its claimed action")
        content = read_completed_return(launch, context.prompt, completion_observed=True)
        if returned.exists() and returned.read_bytes() != content:
            raise review.WorkflowError("conflict", "native return differs from the retained exact bytes", "conflict")
        transport.write_bytes(returned, content)
        return content
    content = _with_context(launch, capture)
    if isinstance(launch.ref, build.BuildRef):
        return transport.capture_build_return(launch.ref, launch.action_id, content, status=status, diagnostic=diagnostic)
    if isinstance(launch.ref, review.ReviewRef):
        return transport.capture_review_return(launch.ref, launch.action_id, content, status=status, diagnostic=diagnostic)
    return transport.capture_measure_return(launch.ref, launch.action_id, content, status=status, diagnostic=diagnostic)
