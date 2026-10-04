"""Codex collaboration launch metadata and owned host-written return bindings."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING

from multiagent import review_workflow as review
from multiagent.native_binding import (
    NativeLaunch,
    _binding_path,
    bind_native_launch,
    require_capture,
)

__all__ = ["NativeLaunch", "bind_native_launch", "launch_metadata", "require_capture"]

if TYPE_CHECKING:
    from multiagent.build_workflow import BuildRef
    from multiagent.measure_twice import MeasureRef

    Ref = review.ReviewRef | MeasureRef | BuildRef


def launch_metadata(
    ref: Ref,
    action_id: str,
    role: str,
    model: str | None,
    effort: str | None,
    prompt_path: str,
    returned_path: str,
) -> dict:
    from multiagent.measure_twice import _canonical, sha256

    if role not in {"crew:reviewer", "crew:panelist", "crew:advisor", "crew:executor"}:
        raise review.WorkflowError(
            "unresolved_native_role", "no shared Codex role prompt resolves"
        )
    if role in {"crew:reviewer", "crew:panelist"} and model in {None, "inherit"}:
        raise review.WorkflowError(
            "unsupported_native_model",
            "Codex reviewer and panelist launches require an explicit model",
        )
    role_path = (
        Path(__file__).resolve().parents[2] / "agents" / f"{role.split(':')[-1]}.md"
    )
    try:
        body = role_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise review.WorkflowError(
            "unresolved_native_role", f"cannot read shared role {role}: {exc}"
        ) from exc
    if body.startswith("---\n"):
        parts = body.split("---\n", 2)
        if len(parts) != 3:
            raise review.WorkflowError(
                "unresolved_native_role", f"invalid shared role {role}"
            )
        body = parts[2]
    spawn = {
        "task_name": "crew_"
        + sha256(_canonical(dataclasses.asdict(ref)) + action_id.encode())[:24],
        "fork_turns": "none",
        "message": f"Adapter role: {role}. Shared role instructions ({role_path}):\n{body}\n"
        "Role access is advisory: you inherit the parent's sandbox and tools. "
        "Follow the issued action's access discipline.\n"
        f"Read {prompt_path} and perform exactly the issued action. "
        "Return your final reply directly; do not invoke another Crew workflow or mutate loop state.",
    }
    if model not in {None, "inherit"}:
        spawn["model"] = model
    if effort is not None:
        spawn["reasoning_effort"] = effort
    return {
        "api": "collaboration",
        "spawn": spawn,
        "returned_path": returned_path,
        "binding_path": str(
            _binding_path(
                Path(prompt_path).parent.parent / "native-transport", action_id
            )
        ),
        "capture": "host_write",
        "model_attribution": "requested-only",
        "continuation": "fresh",
    }
