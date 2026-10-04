"""Owned native action bindings, shared by direct-capture host adapters."""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from multiagent import review_workflow as review

if TYPE_CHECKING:
    from multiagent.build_workflow import BuildRef
    from multiagent.measure_twice import MeasureRef

    Ref = review.ReviewRef | MeasureRef | BuildRef


@dataclass(frozen=True, slots=True)
class NativeLaunch:
    ref: Ref
    action_id: str
    handle: str


def _binding_path(root: Path, action_id: str) -> Path:
    from multiagent.measure_twice import sha256

    path = root / f"{sha256(action_id.encode())}.launch.json"
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise review.WorkflowError(
            "invalid_native_binding", "native binding must be an owned regular file"
        )
    return path


def require_capture(
    root: Path,
    action_id: str,
    handle: str | None,
    completion_observed: bool,
    *,
    status: str = "ok",
    diagnostic: str | None = None,
    launch_refused: bool = False,
    ref: Ref,
) -> None:
    path = _binding_path(root, action_id)
    # A refused spawn has no actual handle and cannot be retried on another route.
    if launch_refused:
        if (
            not path.exists()
            and handle is None
            and not completion_observed
            and status == "failed"
            and isinstance(diagnostic, str)
            and diagnostic.strip()
        ):
            return
        raise review.WorkflowError(
            "invalid_native_binding",
            "launch refusal requires a failed unlaunched action without a handle",
        )
    if completion_observed is not True:
        raise review.WorkflowError(
            "completion_not_observed", "capture requires the actual owned final reply"
        )
    try:
        binding = json.loads(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise review.WorkflowError(
            "invalid_native_binding", "capture requires an owned native launch binding"
        ) from exc
    if (
        not isinstance(binding, dict)
        or binding.get("schema") != 1
        or binding.get("ref") != dataclasses.asdict(ref)
        or not handle
        or binding.get("handle") != handle
        or binding.get("action_id") != action_id
    ):
        raise review.WorkflowError(
            "invalid_native_binding", "completion handle differs from its owned action"
        )

    try:
        prompt = Path(binding["prompt_path"])
        if prompt.is_symlink() or not prompt.is_file():
            raise ValueError("prompt is not a regular file")
        from multiagent.measure_twice import sha256

        if sha256(prompt.read_bytes()) != binding.get("prompt_sha256"):
            raise ValueError("prompt changed after native launch")
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise review.WorkflowError(
            "invalid_native_binding", f"native prompt integrity check failed: {exc}"
        ) from exc


def bind_native_launch(launch: NativeLaunch, *, channel: str = "codex") -> dict:
    if channel not in {"codex", "openhands"}:
        raise review.WorkflowError(
            "invalid_native_binding", "unsupported direct native channel"
        )
    from multiagent import build_workflow as build
    from multiagent import measure_twice as measure

    if (
        not isinstance(launch.handle, str)
        or not launch.handle
        or len(launch.handle) > 512
        or any(c.isspace() or ord(c) < 32 for c in launch.handle)
    ):
        raise review.WorkflowError(
            "invalid_native_binding", "bind requires an actual native runtime handle"
        )

    def bind(root: Path, item: object, claimed: bool) -> dict:
        path = _binding_path(root, launch.action_id)
        body = measure._canonical(
            {
                "schema": 1,
                "ref": dataclasses.asdict(launch.ref),
                "action_id": launch.action_id,
                "handle": launch.handle,
                "prompt_path": item.prompt_path,
                "prompt_sha256": measure.sha256(Path(item.prompt_path).read_bytes()),
            }
        )
        if path.exists():
            if path.read_bytes() != body:
                raise review.WorkflowError(
                    "conflict", "Native launch differs from the owned binding"
                )
        elif not claimed:
            raise review.WorkflowError(
                "invalid_action", "bind requires an already claimed native action"
            )
        else:
            for existing in root.glob("*.launch.json"):
                if existing.is_symlink() or not existing.is_file():
                    raise review.WorkflowError(
                        "invalid_native_binding", "native binding is not a regular file"
                    )
                try:
                    previous = json.loads(existing.read_bytes())
                except (OSError, ValueError) as exc:
                    raise review.WorkflowError(
                        "invalid_native_binding", "native binding is unreadable"
                    ) from exc
                if not isinstance(previous, dict):
                    raise review.WorkflowError(
                        "invalid_native_binding", "native binding is malformed"
                    )
                if previous.get("handle") == launch.handle:
                    raise review.WorkflowError(
                        "conflict",
                        "native handle already belongs to another action",
                    )
            measure._write_once(path, body, root)
        return {"bound": True, "action_id": launch.action_id, "handle": launch.handle}

    if isinstance(launch.ref, build.BuildRef):

        def owned_build(_data: dict, journal: build.BuildJournal) -> dict:
            action = journal.action
            if (
                journal.executor.host != channel
                or journal.executor.role != "crew:executor"
                or action is None
                or action.action_id != launch.action_id
                or action.status not in {"claimed", "settled"}
            ):
                raise review.WorkflowError(
                    "invalid_action", "binding is not the owned native writer"
                )
            if action.handle is not None and action.handle != launch.handle:
                raise review.WorkflowError(
                    "conflict", "native writer already has another handle"
                )
            root = build._safe(
                build._root(launch.ref) / "native-transport" / "anchor", launch.ref
            ).parent
            result = bind(
                root, build._item(launch.ref, journal), action.status == "claimed"
            )
            if action.status == "claimed":
                journal.action = dataclasses.replace(action, handle=launch.handle)
            return result

        return build._transaction(launch.ref, owned_build)
    if isinstance(launch.ref, measure.MeasureRef):

        def owned_measure(_data: dict, journal: measure.MeasureJournal) -> dict:
            action = journal.action
            if (
                journal.advisor_channel != channel
                or action is None
                or action.item.action_id != launch.action_id
                or action.item.driver != "native"
                or action.status not in {"claimed", "promotion_pending"}
            ):
                raise review.WorkflowError(
                    "invalid_action", "binding is not the owned native advisor"
                )
            root = measure._safe_path(
                measure._namespace(launch.ref, journal) / "native-transport",
                measure._namespace(launch.ref, journal),
            )
            return bind(root, action.item, action.status == "claimed")

        return measure._transaction(launch.ref, owned_measure)
    if isinstance(launch.ref, review.ReviewRef):
        run = review._guard_review_path(
            session_segment=launch.ref.session_segment,
            run_id=launch.ref.run_id,
            create=False,
        )
        with review._owned_workflow_lock(run, launch.ref):
            wf, run = review._load_current_locked(launch.ref, run)
            action = review._load_action(wf, launch.action_id)
            if (
                review._action_attempt(action) != launch.ref.attempt_id
                or action["driver"] != "native"
                or action["channel"] != channel
                or action["status"] not in {"claimed", "settled"}
            ):
                raise review.WorkflowError(
                    "invalid_action", "binding is not the owned native reviewer"
                )
            root = review._prepare_run_descendant(
                run,
                run
                / "attempts"
                / launch.ref.attempt_id
                / "native-transport"
                / "anchor",
                "native binding",
                create_parents=True,
            ).parent
            return bind(
                root,
                review._work_item(action, launch.ref),
                action["status"] == "claimed",
            )
    raise review.WorkflowError("invalid_ref", "unsupported Codex action owner")
