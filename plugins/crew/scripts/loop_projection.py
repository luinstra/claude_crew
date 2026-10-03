"""Lightweight lifecycle text. Bound review references remain opaque here."""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from build_state import writer_fence
from models import (
    DEFAULT_MAX_STOP_FIRES,
    effective_count,
    effective_deadline,
    elapsed_minutes,
)
from multiagent.review_runs import session_segment, valid_harness_session_id


@dataclass(frozen=True, slots=True)
class LoopProjection:
    stage: str
    next_argv: tuple[str, ...]
    waiting: bool
    budget: str
    advisory: str | None
    loop: str = "measure-twice"

    def render(self) -> str:
        text = f"Engine-owned {self.loop} stage: {self.stage}. {self.budget}\n"
        if self.advisory:
            text += f"{self.advisory}\n"
        if self.next_argv:
            text += f"Next: {shlex.join(self.next_argv)}\n"
        else:
            text += f"Recovery requires a valid current harness session ID. Resume from that session with crew {self.loop}-resume and its literal session ID.\n"
        text += f"Follow its {'BuildStep' if self.loop == 'build' else 'MeasureStep'}; honor claimed work and human waits. To exit: /crew:cancel-{self.loop}."
        return text


def project_measure(
    data: Mapping[str, object], *, session_id: str = ""
) -> LoopProjection:
    journal = data.get("mt_workflow")
    session = str(data.get("session_id") or session_id)
    executable = str(Path(__file__).resolve().parent.parent / "crew")
    if isinstance(journal, dict):
        stage = (
            "finalizing"
            if data.get("phase") == "done"
            else str(journal.get("stage") or "invalid")
        )
        argv = (
            executable,
            "measure-twice-next",
            "--session-segment",
            session_segment(session),
            "--loop-instance-id",
            str(data.get("loop_instance_id") or ""),
        )
    else:
        stage = "finalizing" if data.get("phase") == "done" else "legacy migration"
        argv = (executable, "measure-twice-resume", "--session-id", session)
    if not valid_harness_session_id(session):
        argv = ()
    fires = effective_count(data.get("stop_fires"), 0)
    maximum = effective_count(data.get("max_stop_fires"), DEFAULT_MAX_STOP_FIRES)
    deadline = effective_deadline(
        data.get("deadline_minutes"), opted_out=data.get("no_deadline") is True
    )
    elapsed = elapsed_minutes(data.get("started_at"))
    budget = f"fires={fires}/{maximum} elapsed={'?' if elapsed is None else f'{elapsed:.0f}'} deadline={deadline}min"
    overrides = data.get("last_verdict_overrides")
    advisory = (
        f"Completion recorded over advisories: {', '.join(overrides)}."
        if overrides
        else None
    )
    if data.get("phase") == "done" and not overrides:
        advisory = "Panel approved; the panel signed off. Finalization only."
    if data.get("awaiting_input") is True:
        advisory = (
            advisory + " " if advisory else ""
        ) + "A bound human question is outstanding."
    return LoopProjection(
        stage, argv, data.get("awaiting_input") is True, budget, advisory
    )


def project_build(
    data: Mapping[str, object], *, session_id: str = ""
) -> LoopProjection:
    journal = data.get("bl_workflow")
    stage = (
        "finalizing"
        if data.get("phase") == "done"
        else str(journal.get("stage") or "invalid")
        if isinstance(journal, dict)
        else "invalid"
    )
    common = project_measure(
        {**data, "mt_workflow": {"stage": stage}}, session_id=session_id
    )
    argv = tuple(
        "build-next" if word == "measure-twice-next" else word
        for word in common.next_argv
    )
    advisory = common.advisory
    fence = writer_fence(data)
    if fence:
        advisory = (advisory + "\n" if advisory else "") + fence
    return replace(common, stage=stage, next_argv=argv, advisory=advisory, loop="build")
