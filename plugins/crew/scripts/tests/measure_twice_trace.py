#!/usr/bin/env python3
"""Reproduce a deterministic transport trace; never exercises the installed app."""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from multiagent import config, measure_twice as mt, review_workflow as review, workflow_transport as transport

TARGET = b"# Plan\nWrite a harmless text file.\n"
REVIEW = b"## VERDICT\nAPPROVED\n\n## FINDINGS\nnone\n"


@dataclass
class TraceRuntime:
    events: list[str] = field(default_factory=list)
    items: dict[str, mt.MeasureWorkItem] = field(default_factory=dict)
    intervals: list[dict[str, object]] = field(default_factory=list)

    def launch(self, item: mt.MeasureWorkItem) -> str:
        if item.role == "crew:scribe":
            raise AssertionError("direct capture must never launch a scribe")
        self.events.append(f"launch:{item.kind}")
        self.items[item.action_id] = item
        return item.action_id

    def completions(self, handles: tuple[str, ...]) -> Iterable[transport.Completion]:
        if len(handles) == 2 and self.events[-2:] != ["launch:reviewer", "launch:reviewer"]:
            raise AssertionError("the entire reviewer batch must launch before any wait")
        self.events.append("wait:notifications")
        for end, handle in enumerate(handles, 1):
            item = self.items[handle]
            self.intervals.append({"kind": item.kind, "model": item.model, "start": 0, "end": end})
            self.events.append(f"return:{item.kind}")
            if item.kind == "advisor":
                Path(item.staging_path).write_bytes(TARGET)
                yield transport.Completion(handle, b"plan written")
            elif item.kind == "synthesis":
                yield transport.Completion(handle, b"Approved\nBLOCKING_CAUSES: 0\n",
                    judgment=transport.SynthesisJudgment("APPROVED"))
            else:
                yield transport.Completion(handle, REVIEW)


def trace() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as task_home:
        root = Path(project).resolve()
        with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(root), "HOME": task_home, "CREW_HOST": "claude"}):
            config._reset_cache_for_tests()
            try:
                (root / "requirements.md").write_text("Plan one harmless text file. Do not implement it.")
                runtime = TraceRuntime()
                step = mt.start_measure_twice(mt.MeasureRequest("--seats opus,sonnet requirements.md", "trace"))
                response_types = [str(step.type)]
                for _batch in range(3):
                    step = transport.run_measure_batch(step, runtime)
                    response_types.append(str(step.type))
                if step.type != "terminal" or step.outcome["status"] != "approved":
                    raise AssertionError("fixture did not complete")
                before = len(runtime.events)
                replay = mt.next_measure_twice(step.ref)
                if replay != step or len(runtime.events) != before:
                    raise AssertionError("terminal replay issued paid work")
                counts = {kind: runtime.events.count(f"launch:{kind}")
                          for kind in ("advisor", "reviewer", "formatter")}
                counts.update(scribe=0, parent_synthesis=runtime.events.count("launch:synthesis"))
                claude_scribes = sum(1 for item in runtime.items.values()
                    if item.return_transport and item.return_transport.get("primary", {}).get("kind") == "scribe")
                return {
                    "source_baseline": "b39f647", "evidence_kind": "deterministic_public_api_runtime",
                    "scenario": "document -> advisor -> two native reviewers -> synthesis -> approve",
                    "target": {"utf8": TARGET.decode(), "sha256": mt.sha256(TARGET)},
                    "panel": {"seats": ["opus", "sonnet"], "pins": ["opus", "sonnet"]},
                    "response_types": response_types, "events": runtime.events,
                    "counts": counts,
                    "intervals": runtime.intervals, "clock": "simulated units, not observed provider wall time",
                    "terminal_replay_paid_work": len(runtime.events) - before, "live_costs": None,
                    "claude_markdown_scribes": claude_scribes,
                    "limits": "The direct runtime is a deterministic fake. Claude retains reviewer scribes; no app gate or live token/latency claim.",
                }
            finally:
                config._reset_cache_for_tests()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    body = json.dumps(trace(), indent=2) + "\n"
    if args.output:
        args.output.write_text(body)
    else:
        sys.stdout.write(body)


if __name__ == "__main__":
    main()
