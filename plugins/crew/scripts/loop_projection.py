"""Lightweight lifecycle text. Bound review references remain opaque here."""
from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Mapping

from models import effective_count, effective_deadline, elapsed_minutes, DEFAULT_MAX_STOP_FIRES
from multiagent.review_runs import session_segment, valid_harness_session_id


@dataclass(frozen=True, slots=True)
class LoopProjection:
    stage: str
    next_argv: tuple[str, ...]
    waiting: bool
    budget: str
    advisory: str | None

    def render(self) -> str:
        text = f"Engine-owned measure-twice stage: {self.stage}. {self.budget}\n"
        if self.advisory:
            text += f"{self.advisory}\n"
        if self.next_argv:
            text += f"Next: {shlex.join(self.next_argv)}\n"
        else:
            text += "Recovery requires a valid current harness session ID. Resume from that session with crew measure-twice-resume and its literal --session-id.\n"
        text += "Follow its MeasureStep; honor claimed work and human waits. To exit: /crew:cancel-measure-twice."
        return text


def project_measure(data: Mapping[str, object], *, session_id: str = "") -> LoopProjection:
    journal = data.get("mt_workflow")
    session = str(data.get("session_id") or session_id)
    executable = str(Path(__file__).resolve().parent.parent / "crew")
    if isinstance(journal, dict):
        stage = "finalizing" if data.get("phase") == "done" else str(journal.get("stage") or "invalid")
        argv = (executable, "measure-twice-next", "--session-segment", session_segment(session),
                "--loop-instance-id", str(data.get("loop_instance_id") or ""))
    else:
        stage = "finalizing" if data.get("phase") == "done" else "legacy migration"
        argv = (executable, "measure-twice-resume", "--session-id", session)
    if not valid_harness_session_id(session):
        argv = ()
    fires = effective_count(data.get("stop_fires"), 0)
    maximum = effective_count(data.get("max_stop_fires"), DEFAULT_MAX_STOP_FIRES)
    deadline = effective_deadline(data.get("deadline_minutes"), opted_out=data.get("no_deadline") is True)
    elapsed = elapsed_minutes(data.get("started_at"))
    budget = f"fires={fires}/{maximum} elapsed={'?' if elapsed is None else f'{elapsed:.0f}'} deadline={deadline}min"
    overrides = data.get("last_verdict_overrides")
    advisory = f"Completion recorded over advisories: {', '.join(overrides)}." if overrides else None
    if data.get("phase") == "done" and not overrides:
        advisory = "Panel approved; the panel signed off. Finalization only."
    if data.get("awaiting_input") is True:
        advisory = (advisory + " " if advisory else "") + "A bound human question is outstanding."
    return LoopProjection(stage, argv, data.get("awaiting_input") is True, budget, advisory)
