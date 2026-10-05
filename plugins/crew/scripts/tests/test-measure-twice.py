#!/usr/bin/env python3
"""Public planning-loop contracts with isolated state and deterministic returns."""
from __future__ import annotations

import dataclasses
import concurrent.futures
import shutil
import importlib
import threading
import time
import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import loop_state
import artifact_prune
import models
from multiagent import config, seats, measure_twice as mt, review_workflow as rw, workflow_transport as transport
from multiagent import claude_native_transport as native

VALID = b"## VERDICT\nAPPROVED\n\n## FINDINGS\nnone\n"


class MeasureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.project = tempfile.TemporaryDirectory()
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.project.cleanup)
        self.addCleanup(self.home.cleanup)
        self.root = Path(self.project.name).resolve()
        self.env = mock.patch.dict(os.environ, {"CREW_PROJECT_DIR": "", "CLAUDE_PROJECT_DIR": str(self.root), "HOME": self.home.name,
                                                "CREW_HOST": "claude"})
        self.env.start()
        self.addCleanup(self.env.stop)
        config._reset_cache_for_tests()
        self.addCleanup(config._reset_cache_for_tests)
        (self.root / "requirements.md").write_text("Plan one harmless text file. Do not implement it.")
        self.request = mt.MeasureRequest("--seats opus,sonnet requirements.md", "isolated")

    def start(self) -> mt.MeasureStep:
        return mt.start_measure_twice(self.request)

    def draft(self, step: mt.MeasureStep | None = None) -> mt.MeasureStep:
        step = step or self.start()
        item = step.work_items[0]
        mt.claim_measure_action(step.ref, item.action_id)
        Path(item.staging_path).write_bytes(b"# Plan\nWrite a harmless text file.\n")
        return transport.capture_measure_return(step.ref, item.action_id, b"plan written")

    def panel(self, step: mt.MeasureStep, verdict: str = "APPROVED", minor_only: bool = False,
              failed: int = 0, synthesis_fail: bool = False, blocking: int | None = 1) -> mt.MeasureStep:
        for index, item in enumerate(step.work_items):
            rw.claim_review_action(rw.ClaimRequest(item.review_ref, item.action_id))
            transport.capture_review_return(item.review_ref, item.action_id, VALID,
                status="failed" if index < failed else "ok", diagnostic="outage" if index < failed else None)
        next_step = mt.next_measure_twice(step.ref)
        if next_step.type == "work_batch" and next_step.work_items[0].kind == "synthesis":
            item = next_step.work_items[0]
            rw.claim_review_action(rw.ClaimRequest(item.review_ref, item.action_id))
            text = "Synthesis\n" + (f"BLOCKING_CAUSES: {blocking}\n" if blocking is not None else "Count unknown\n")
            transport.capture_review_return(item.review_ref, item.action_id, text.encode(),
                status="failed" if synthesis_fail else "ok",
                judgment=None if synthesis_fail else transport.SynthesisJudgment(verdict, minor_only),
                diagnostic="lost synthesis" if synthesis_fail else None)
            next_step = mt.next_measure_twice(step.ref)
        return next_step

    def state(self) -> dict[str, object]:
        return loop_state.read(loop_state.resolve("mt", "isolated"))

    def filesystem_snapshot(self) -> dict[str, bytes | None]:
        return {str(path.relative_to(self.root)): path.read_bytes() if path.is_file() else None
                for path in self.root.rglob("*")}

    def test_invalid_session_admission_refuses_before_state_resolution_or_writes(self) -> None:
        for legacy in (False, True):
            if legacy:
                models.atomic_write_json(self.root / ".crew" / "measure-twice-state.json",
                    {"schema": 3, "active": True, "session_id": "", "task": "preserved"})
            for session, code in (("", "missing_session_id"), (" \t", "missing_session_id"),
                                  ("../../", "invalid_session_id"), ("!!", "invalid_session_id"),
                                  ("<session-id>", "invalid_session_id")):
                with self.subTest(legacy=legacy, session=session):
                    before = self.filesystem_snapshot()
                    with mock.patch.object(loop_state, "resolve", wraps=loop_state.resolve) as resolve, self.assertRaises(rw.WorkflowError) as refused:
                        mt.start_measure_twice(dataclasses.replace(self.request, session_id=session))
                    self.assertEqual(refused.exception.code, code)
                    resolve.assert_not_called()
                    self.assertEqual(self.filesystem_snapshot(), before)

    def test_cli_invalid_session_preserves_request_and_all_files(self) -> None:
        spill = self.root / "request.json"
        spill.write_text(json.dumps({"schema": 1, "raw_arguments": self.request.raw_arguments, "requirements": None}))
        for legacy in (False, True):
            if legacy:
                models.atomic_write_json(self.root / ".crew" / "measure-twice-state.json",
                    {"schema": 3, "active": True, "session_id": "", "task": "preserved"})
            for session, code in (("", "missing_session_id"), ("../../", "invalid_session_id"),
                                  ("!!", "invalid_session_id"), ("<session-id>", "invalid_session_id")):
                for command in ("measure-twice", "measure-twice-resume"):
                    with self.subTest(legacy=legacy, session=session, command=command):
                        before = self.filesystem_snapshot()
                        args = ("-f", str(spill), "--consume") if command == "measure-twice" else ()
                        result = self.cli(command, *args, "--session-id", session)
                        self.assertEqual(result.returncode, 2, result.stderr)
                        self.assertEqual(json.loads(result.stdout)["code"], code)
                        self.assertEqual(self.filesystem_snapshot(), before)

    def test_valid_session_segments_and_normal_sanitization_are_preserved(self) -> None:
        for session, segment in (("_", "_"), ("AbC_12-34", "AbC_12-34"),
                                 (" ../../AbC_12-34 ", "AbC_12-34")):
            with self.subTest(session=session):
                step = mt.start_measure_twice(dataclasses.replace(self.request, session_id=session))
                self.assertEqual(step.ref.session_segment, segment)
                self.assertTrue((self.root / ".crew" / f"measure-twice-state-{segment}.json").is_file())
                mt.cancel_measure_twice(step.ref, "cleanup")
                shutil.rmtree(self.root / ".crew")

    def test_normal_terminal_replay_and_different_request_new_lifetime(self) -> None:
        for finish in ("approved", "cancelled", "review_failed"):
            with self.subTest(finish=finish):
                first = self.start()
                if finish == "approved":
                    terminal = self.panel(self.draft(first))
                elif finish == "review_failed":
                    terminal = self.panel(self.panel(self.draft(first), failed=2), failed=2)
                else:
                    terminal = mt.cancel_measure_twice(first.ref, "fixture cancel")
                self.assertEqual(terminal.outcome["status"], finish)
                self.assertEqual(self.start(), terminal)
                other = self.root / "other.md"
                other.write_text("A different immutable request.")
                fresh = mt.start_measure_twice(dataclasses.replace(self.request, raw_arguments="--seats opus,sonnet other.md"))
                self.assertEqual(fresh.type, "work_batch")
                self.assertNotEqual(fresh.ref, first.ref)
                self.assertEqual(self.state()["stop_fires"], 0)
                with self.assertRaises(rw.WorkflowError):
                    mt.claim_measure_action(first.ref, first.work_items[0].action_id)
                mt.cancel_measure_twice(fresh.ref, "cleanup")
                loop_state.resolve("mt", "isolated").unlink()

    def test_admission_cannot_replace_a_racing_lifetime(self) -> None:
        old = self.start()
        mt.cancel_measure_twice(old.ref, "normal terminal")
        request = dataclasses.replace(self.request, raw_arguments="--seats opus other.md")
        (self.root / "other.md").write_text("New request")
        real_initialize = loop_state.initialize
        def replace_before_admission(*args: object, **kwargs: object) -> dict[str, object]:
            loop_state.mutate(loop_state.resolve("mt", "isolated"), lambda data: {
                **data, "active": True, "loop_instance_id": "racing-owner"})
            return real_initialize(*args, **kwargs)
        with mock.patch.object(loop_state, "initialize", side_effect=replace_before_admission), self.assertRaises((rw.WorkflowError, loop_state.LoopStateError)):
            mt.start_measure_twice(request)
        self.assertEqual(self.state()["loop_instance_id"], "racing-owner")
        self.assertTrue(self.state()["active"])

    def test_admission_checks_scoped_and_adoptable_legacy_conflicts_independently(self) -> None:
        directory = self.root / ".crew"
        directory.mkdir()
        for prefix in ("build-state", "measure-twice-state"):
            for schema, active in ((3, True), (99, False)):
                with self.subTest(prefix=prefix, schema=schema):
                    scoped = directory / f"{prefix}-isolated.json"
                    legacy = directory / f"{prefix}.json"
                    models.atomic_write_json(scoped, {"schema": 3, "active": False})
                    models.atomic_write_json(legacy, {"schema": schema, "active": active, "session_id": ""})
                    before = legacy.read_bytes()
                    with self.assertRaises((rw.WorkflowError, loop_state.LoopStateError)):
                        self.start()
                    self.assertEqual(legacy.read_bytes(), before)
                    scoped.unlink(); legacy.unlink()

    def test_hidden_legacy_safety_exit_requires_explicit_restart(self) -> None:
        directory = self.root / ".crew"
        directory.mkdir()
        scoped = directory / "measure-twice-state-isolated.json"
        legacy = directory / "measure-twice-state.json"
        for owner in ("", "isolated"):
            for marker in ({"force_exit": True}, {"exit_kind": "force_exit"}):
                with self.subTest(owner=owner, marker=marker):
                    models.atomic_write_json(scoped, {"schema": 3, "active": False, "session_id": "isolated", "task": "previous request"})
                    models.atomic_write_json(legacy, {"schema": 3, "active": False, "session_id": owner,
                        "stop_fires": 150, "started_at": "2000-01-01T00:00:00+00:00", **marker})
                    before = (scoped.read_bytes(), legacy.read_bytes())
                    with self.assertRaises((rw.WorkflowError, loop_state.LoopStateError)) as refused:
                        self.start()
                    self.assertEqual(refused.exception.code, "active_request_conflict")
                    spill = self.root / "request.json"
                    spill.write_text(json.dumps({"schema": 1, "raw_arguments": self.request.raw_arguments, "requirements": None}))
                    result = self.cli("measure-twice", "-f", str(spill), "--consume", "--session-id", "isolated")
                    self.assertEqual(result.returncode, 2, result.stdout)
                    self.assertEqual((scoped.read_bytes(), legacy.read_bytes()), before)
                    self.assertTrue(spill.is_file())
                    self.assertFalse((directory / "plans").exists())
        # Compatibility init remains the explicit restart authority.
        restarted = self.cli("state", "init", "mt", "--auto-plan", "--task", self.request.raw_arguments,
                             "--session-id", "isolated", "--force")
        self.assertEqual(restarted.returncode, 0, restarted.stdout + restarted.stderr)
        resumed = self.start()
        self.assertEqual(resumed.question.kind, "legacy_work_not_running")
        self.assertEqual(self.state()["stop_fires"], 0)
        self.assertEqual(self.start().ref, resumed.ref)

    def test_hidden_legacy_safety_exit_in_other_session_is_isolated(self) -> None:
        directory = self.root / ".crew"
        directory.mkdir()
        legacy = directory / "measure-twice-state.json"
        models.atomic_write_json(directory / "measure-twice-state-isolated.json", {"schema": 3, "active": False})
        models.atomic_write_json(legacy, {"schema": 3, "active": False, "session_id": "another", "force_exit": True})
        before = legacy.read_bytes()
        self.assertEqual(self.start().type, "work_batch")
        self.assertEqual(legacy.read_bytes(), before)

    def test_unowned_legacy_hook_guidance_executes_with_current_session(self) -> None:
        path = self.root / ".crew" / "measure-twice-state.json"
        for hook in ("session-start.py", "persistent-mode.py"):
            for phase in ("drafting", "done"):
                with self.subTest(hook=hook, phase=phase):
                    models.atomic_write_json(path, {"schema": 3, "loop": "mt", "active": True,
                        "session_id": "", "loop_instance_id": "unowned-hook", "task": self.request.raw_arguments,
                        "phase": phase, "last_verdict": "APPROVED" if phase == "done" else "",
                        "started_at": models.utc_now_iso()})
                    before = path.read_bytes()
                    payload = {"session_id": "isolated", "cwd": str(self.root), "permission_mode": "default",
                               "hook_event_name": "SessionStart" if hook == "session-start.py" else "Stop"}
                    result = subprocess.run([sys.executable, str(Path(mt.__file__).parents[1] / hook)],
                        input=json.dumps(payload), cwd=self.root, env=dict(os.environ), capture_output=True, text=True, timeout=7)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    response = json.loads(result.stdout)
                    text = response["hookSpecificOutput"]["additionalContext"] if hook == "session-start.py" else response["reason"]
                    lines = [line.removeprefix("Next: ") for line in text.splitlines() if line.startswith("Next: ")]
                    self.assertEqual(len(lines), 1, text)
                    argv = shlex.split(lines[0])
                    self.assertEqual(argv[-2:], ["--session-id", "isolated"])
                    self.assertEqual(json.loads(path.read_bytes())["session_id"], "")
                    if hook == "session-start.py":
                        self.assertEqual(path.read_bytes(), before)
                    resumed = self.cli(*argv[1:])
                    self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
                    step = json.loads(resumed.stdout)
                    self.assertEqual(step["type"], "terminal" if phase == "done" else "needs_input")
                    self.assertEqual(step["ref"]["session_segment"], "isolated")
                    path.unlink()

    def test_projection_never_issues_invalid_session_or_overrides_an_owner(self) -> None:
        from loop_projection import project_measure
        for session in ("", "!!", "../../", "<session-id>"):
            projection = project_measure({"session_id": "", "active": True}, session_id=session)
            self.assertEqual(projection.next_argv, ())
            self.assertNotIn("Next:", projection.render())
            self.assertIn("valid current harness session ID", projection.render())
        self.assertEqual(project_measure({"session_id": "owned"}, session_id="other").next_argv[-1], "owned")
        self.assertEqual(project_measure({"session_id": "!!"}, session_id="other").next_argv, ())

    def test_synthesis_failure_question_identity_and_strict_retry_kinds(self) -> None:
        wait = self.panel(self.draft(), synthesis_fail=True)
        old = mt.MeasureDecision(wait.ref, wait.question.question_id, "retry_synthesis")
        with self.assertRaises(rw.WorkflowError) as refused:
            mt.decide_measure_twice(wait.ref, dataclasses.replace(old, kind="retry_review"))
        self.assertEqual(refused.exception.code, "invalid_decision")
        retry = mt.decide_measure_twice(wait.ref, old)
        work = retry.work_items[0]
        rw.claim_review_action(rw.ClaimRequest(work.review_ref, work.action_id))
        transport.capture_review_return(work.review_ref, work.action_id, b"second failed synthesis", status="failed", diagnostic="second failure")
        second = mt.next_measure_twice(wait.ref)
        self.assertNotEqual(second.question.question_id, wait.question.question_id)
        before = self.state()
        with self.assertRaises(rw.WorkflowError) as stale:
            mt.decide_measure_twice(wait.ref, old)
        self.assertEqual(stale.exception.code, "stale_question")
        self.assertEqual(self.state(), before)
        mt.cancel_measure_twice(wait.ref, "cleanup")
        loop_state.resolve("mt", "isolated").unlink()
        advisory = self.panel(self.draft(), failed=1)
        with self.assertRaises(rw.WorkflowError) as refused:
            mt.decide_measure_twice(advisory.ref, mt.MeasureDecision(advisory.ref, advisory.question.question_id, "retry_synthesis"))
        self.assertEqual(refused.exception.code, "invalid_decision")

    def test_force_decision_obeys_expired_bounds_but_cancel_remains_available(self) -> None:
        for bound in ({"stop_fires": 150}, {"started_at": "2000-01-01T00:00:00+00:00", "deadline_minutes": 1}):
            for kind in ("force", "cancel"):
                with self.subTest(bound=bound, kind=kind):
                    wait = self.panel(self.draft(), failed=1)
                    before = self.state()["mt_workflow"]["applied_outcomes"]
                    loop_state.mutate(loop_state.resolve("mt", "isolated"), lambda data: {**data, **bound})
                    terminal = mt.decide_measure_twice(wait.ref, mt.MeasureDecision(wait.ref, wait.question.question_id, kind, "force" if kind=="force" else None))
                    self.assertEqual(terminal.outcome["status"], "force_exit" if kind=="force" else "cancelled")
                    self.assertEqual(self.state()["mt_workflow"]["applied_outcomes"], before)
                    self.assertNotEqual(self.state()["phase"], "done")
                    loop_state.resolve("mt", "isolated").unlink()

    def test_completed_advisor_invalid_plan_settles_and_replays_original_return(self) -> None:
        for plan in (None, b"", b" \n", b"\xff", "directory"):
            with self.subTest(plan=plan):
                step = self.start(); item = step.work_items[0]
                mt.claim_measure_action(step.ref, item.action_id)
                path = Path(item.staging_path)
                if plan == "directory": path.mkdir()
                elif plan is not None: path.write_bytes(plan)
                raw = b"Actual completed report without final LF"
                wait = transport.capture_measure_return(step.ref, item.action_id, raw)
                self.assertEqual(wait.question.kind, "advisor_retry")
                journal = mt.journal_from_dict(self.state()["mt_workflow"])
                self.assertEqual(journal.action.status, "settled")
                self.assertEqual(journal.accepted_actions[-1].status, "failed")
                self.assertEqual(journal.accepted_actions[-1].returned_sha256, mt.sha256(raw))
                if path.is_dir(): path.rmdir()
                path.write_bytes(b"# Later repair cannot change the observed failure")
                self.assertEqual(transport.capture_measure_return(step.ref, item.action_id, raw), wait)
                with self.assertRaises(rw.WorkflowError):
                    transport.capture_measure_return(step.ref, item.action_id, raw+b"changed")
                retry = mt.decide_measure_twice(wait.ref, mt.MeasureDecision(wait.ref, wait.question.question_id, "retry_advisor"))
                self.assertNotEqual(retry.work_items[0].action_id, item.action_id)
                mt.cancel_measure_twice(step.ref, "cleanup")
                loop_state.resolve("mt", "isolated").unlink()

    def test_missing_state_next_is_a_typed_public_error(self) -> None:
        result = self.cli("measure-twice-next", "--session-segment", "isolated", "--loop-instance-id", "missing")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(json.loads(result.stdout)["code"], "missing_state")

    def test_genuine_legacy_without_loop_discriminator_resumes(self) -> None:
        models.atomic_write_json(loop_state.resolve("mt", "isolated"), {
            "schema": 2, "active": True, "task_description": "--seats opus old task",
            "session_id": "isolated", "stop_fires": 7})
        wait = self.start()
        self.assertEqual(wait.question.kind, "legacy_work_not_running")
        self.assertEqual(self.confirm_legacy(wait).work_items[0].kind, "advisor")
        self.assertEqual(self.state()["stop_fires"], 7)

    def test_legacy_requirements_recovery_adopts_retained_plan(self) -> None:
        for phase, task, kind in (("reviewing", "missing.md", "retry_requirements"), ("drafting", "", "requirements")):
            wait = self.legacy(phase, task=task, content=b"# Existing legacy plan")
            needs = self.confirm_legacy(wait)
            self.assertEqual(needs.question.kind, "legacy_requirements")
            if kind == "retry_requirements":
                (self.root / "missing.md").write_text("Repaired requirements")
                decision = mt.MeasureDecision(needs.ref, needs.question.question_id, kind)
            else:
                decision = mt.MeasureDecision(needs.ref, needs.question.question_id, kind,
                    requirements=mt.CapturedRequirements(needs.question.question_id, ("scope", "limits", "checks")))
            resumed = mt.decide_measure_twice(needs.ref, decision)
            self.assertEqual(resumed.work_items[0].kind, "reviewer")
            self.assertEqual(self.state()["revision_round"], 2)
            loop_state.resolve("mt", "isolated").unlink()

    def test_legacy_requirements_recovery_keeps_revision_feedback_guard(self) -> None:
        for verdict in ("REVISE", "REJECT"):
            wait = self.legacy(task="missing.md", verdict=verdict, content=b"# Retained prior plan")
            needs = self.confirm_legacy(wait)
            self.assertEqual(needs.question.kind, "legacy_requirements")
            (self.root / "missing.md").write_text("Repaired source")
            resumed = mt.decide_measure_twice(needs.ref, mt.MeasureDecision(
                needs.ref, needs.question.question_id, "retry_requirements"))
            self.assertEqual(resumed.question.kind, "legacy_plan_recovery")
            self.assertEqual(self.state()["last_verdict"], verdict)
            self.assertEqual(self.state()["revision_round"], 2)
            loop_state.resolve("mt", "isolated").unlink()
            (self.root / "missing.md").unlink()

    def test_provider_probe_releases_owner_lock_and_revalidates_cancellation(self) -> None:
        self.request = dataclasses.replace(self.request, raw_arguments="--seats sol requirements.md")
        panel = self.draft(); item = panel.work_items[0]
        entered, release = threading.Event(), threading.Event()
        provider = mock.Mock()
        def probe() -> tuple[bool, str]:
            entered.set(); release.wait(6)
            return True, "available"
        provider.is_available.side_effect = probe
        errors: list[rw.WorkflowError] = []
        def execute() -> None:
            try: rw.execute_external_review(item.review_ref, item.action_id)
            except rw.WorkflowError as error: errors.append(error)
        with mock.patch.object(rw, "_frozen_external_provider", return_value=provider):
            thread = threading.Thread(target=execute); thread.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(self.default_stop()["decision"], "block")
                self.assertEqual(mt.cancel_measure_twice(panel.ref, "cancel during probe").outcome["status"], "cancelled")
            finally:
                release.set(); thread.join(8)
        self.assertFalse(thread.is_alive())
        provider.run.assert_not_called()
        self.assertEqual([error.code for error in errors], ["stale_owner"])

    def test_review_preparation_probe_releases_lock_and_cannot_commit_after_cancel(self) -> None:
        self.request = dataclasses.replace(self.request, raw_arguments="--seats sol requirements.md")
        ready = self.start()
        item = ready.work_items[0]
        mt.claim_measure_action(ready.ref, item.action_id)
        Path(item.staging_path).write_bytes(b"# Prepared plan")
        entered, release = threading.Event(), threading.Event()
        errors: list[rw.WorkflowError] = []
        def timeout(*args: object) -> int:
            entered.set()
            release.wait(6)
            return 540
        def capture() -> None:
            try:
                transport.capture_measure_return(ready.ref, item.action_id, b"completed")
            except rw.WorkflowError as error:
                errors.append(error)
        with mock.patch.object(rw, "_provider_timeout", side_effect=timeout):
            thread = threading.Thread(target=capture)
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(self.default_stop()["decision"], "block")
                mt.cancel_measure_twice(ready.ref, "cancel while resolving timeout")
            finally:
                release.set()
                thread.join(8)
        self.assertFalse(thread.is_alive())
        self.assertEqual([error.code for error in errors], ["stale_owner"])
        journal = mt.journal_from_dict(self.state()["mt_workflow"])
        self.assertEqual(journal.review_generation, 0)
        self.assertIsNone(journal.pending_review_inputs)
        self.assertFalse(list((self.root / ".crew" / "reviews").rglob("workflow.json")))

    def test_latest_plan_selects_only_canonical_lifetime_files(self) -> None:
        self.draft()
        canonical = Path(self.state()["plan_file"])
        namespace = canonical.parent
        for relative in ("staging/plan-999.md", "sealed/plan-999.md", "raw/plan-999.md", "synthesis.md"):
            path = namespace / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("Newer transport artifact")
        outside = self.root / "outside.md"
        outside.write_text("Newer foreign plan")
        (namespace / "plan-888.md").symlink_to(outside)
        foreign = namespace.parent / "foreign-00000000-0000-0000-0000-000000000001"
        foreign.symlink_to(namespace, target_is_directory=True)
        step = rw.start_review(rw.ReviewRequest("latest plan", seats="opus", session_id="latest-discovery"))
        run = self.root / ".crew" / "reviews" / "latest-discovery" / step.ref.run_id
        record = rw.review_runs.read_run_json(run)
        self.assertEqual(record["target_spec"], canonical.relative_to(self.root).as_posix())
        self.assertEqual(record["target_sha256"], mt.sha256(canonical.read_bytes()))

    def test_advisor_feedback_includes_actual_full_panel_path(self) -> None:
        revision = self.panel(self.draft(), verdict="REVISE")
        item = revision.work_items[0]
        journal = mt.journal_from_dict(self.state()["mt_workflow"])
        self.assertTrue(Path(journal.full_review_path).is_file())
        self.assertIn(journal.full_review_path, Path(item.prompt_path).read_text())

    def test_pending_review_binding_is_loop_owned_and_cannot_certify_outcome(self) -> None:
        original = loop_state.bind_review
        captured: list[loop_state.ReviewEvidence] = []
        def bind(data: dict[str, object], evidence: loop_state.ReviewEvidence) -> None:
            captured.append(evidence)
            original(data, evidence)
        with mock.patch.object(loop_state, "bind_review", side_effect=bind):
            panel = self.draft()
        evidence = captured[0]
        self.assertIsInstance(evidence.source, loop_state.LoopReviewSource)
        self.assertEqual(evidence.source.ref, panel.work_items[0].review_ref)
        self.assertEqual(evidence.source.binding.loop_instance_id, panel.ref.loop_instance_id)
        self.assertIsNone(evidence.source.accepted_outcome_sha256)
        with self.assertRaises(loop_state._Refusal):
            loop_state.apply_verdict(self.state(), evidence, "FAILED")

    def test_rejected_synthesis_guidance_matches_each_workflow_verdict_set(self) -> None:
        for loop_owned in (True, False):
            batch = (self.draft() if loop_owned else rw.start_review(rw.ReviewRequest(
                "requirements.md", seats="opus", session_id="standalone-diagnostic")))
            for item in batch.work_items:
                ref = item.review_ref if loop_owned else batch.ref
                rw.claim_review_action(rw.ClaimRequest(ref, item.action_id))
                transport.capture_review_return(ref, item.action_id, VALID)
            synthesis = mt.next_measure_twice(batch.ref) if loop_owned else rw.next_review(batch.ref)
            item = synthesis.work_items[0]
            ref = item.review_ref if loop_owned else synthesis.ref
            rw.claim_review_action(rw.ClaimRequest(ref, item.action_id))
            with self.assertRaises(rw.WorkflowError) as invalid:
                transport.capture_review_return(ref, item.action_id, b"invalid approval",
                    judgment=transport.SynthesisJudgment("APPROVED", True))
            self.assertEqual(invalid.exception.code, "invalid_submission")
            self.assertEqual("REJECT" in str(invalid.exception), loop_owned)

    def test_projection_sanitizes_journal_session_but_retains_legacy_argument(self) -> None:
        from loop_projection import project_measure
        hostile = "foreign/../session with spaces"
        ready = self.start()
        state = self.state()
        state["session_id"] = hostile
        journal_argv = project_measure(state).next_argv
        self.assertIn(rw.review_runs.session_segment(hostile), journal_argv)
        self.assertNotIn(hostile, journal_argv)
        state.pop("mt_workflow")
        legacy_argv = project_measure(state).next_argv
        self.assertIn(hostile, legacy_argv)

    def native_artifact(self, ref: mt.MeasureRef | rw.ReviewRef, action_id: str,
                        prompt: str, content: str = "Exact return without final LF") -> native.NativeLaunch:
        host_root = Path("/tmp").resolve() / f"claude-{os.getuid()}"
        host_root.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=host_root)
        self.addCleanup(temporary.cleanup)
        output = Path(temporary.name) / ref.session_segment / "tasks" / "a123456789abcdef.output"
        output.parent.mkdir(parents=True)
        launch = native.NativeLaunch(ref, action_id, "a123456789abcdef", output)
        metadata = {"agentId": launch.handle, "sessionId": ref.session_segment,
                    "cwd": str(self.root), "version": "2.1.287"}
        records = [
            {**metadata, "type": "user", "message": {"role": "user", "content": native.launch_prompt(Path(prompt))}},
            {**metadata, "type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "name": "SubagentHandback", "input": {"message": content}}]}},
        ]
        output.write_bytes(b"".join(json.dumps(record, ensure_ascii=False).encode() + b"\n" for record in records))
        return launch

    def test_native_advisor_exact_eof_and_owned_replay(self) -> None:
        step = self.start()
        item = step.work_items[0]
        mt.claim_measure_action(step.ref, item.action_id)
        Path(item.staging_path).write_bytes(b"# Native plan")
        raw = "Native report\r\nUnicode snowman: \u2603; no final LF"
        launch = self.native_artifact(step.ref, item.action_id, item.prompt_path, raw)
        self.assertTrue(native.bind_native_launch(launch)["bound"])
        review = native.capture_native_return(launch, completion_observed=True)
        self.assertEqual(review.type, "work_batch")
        self.assertEqual(mt.journal_from_dict(self.state()["mt_workflow"]).accepted_actions[0].returned_sha256,
                         mt.sha256(raw.encode()))
        self.assertEqual((Path(item.returned_path).parents[1] / "host-return" / f"{item.action_id}.txt").read_bytes(), raw.encode())
        launch.output_file.unlink()
        self.assertEqual(native.capture_native_return(launch, completion_observed=True), review)

    def test_native_reviewer_capture_exact_bytes_and_public_cli_replay(self) -> None:
        review = self.draft()
        item = review.work_items[0]
        rw.claim_review_action(rw.ClaimRequest(item.review_ref, item.action_id))
        raw = VALID.decode().rstrip("\n")
        launch = self.native_artifact(item.review_ref, item.action_id, item.prompt_path, raw)
        flags = ("--session-segment", item.review_ref.session_segment, "--run-id", item.review_ref.run_id,
                 "--attempt-id", item.review_ref.attempt_id, "--target-sha256", item.review_ref.target_sha256,
                 "--action-id", item.action_id, "--handle", launch.handle, "--output-file", str(launch.output_file))
        bound = self.cli("review-native-bind", *flags)
        self.assertEqual(bound.returncode, 0, bound.stdout)
        refused = self.cli("review-native-capture", *flags)
        self.assertEqual(refused.returncode, 2)
        self.assertIn("completion_not_observed", refused.stdout)
        captured = self.cli("review-native-capture", *flags, "--completion-observed")
        self.assertEqual(captured.returncode, 0, captured.stdout)
        fallback = Path(item.return_transport["fallback"]["ingress_path"])
        self.assertEqual(fallback.read_bytes(), raw.encode())
        launch.output_file.unlink()
        self.assertEqual(self.cli("review-native-capture", *flags, "--completion-observed").returncode, 0)
        next_step = mt.next_measure_twice(review.ref)
        self.assertEqual([work.action_id for work in next_step.work_items], [review.work_items[1].action_id])

    def test_native_capture_accepts_only_exact_observed_host_transcript_alias(self) -> None:
        step = self.start()
        item = step.work_items[0]
        mt.claim_measure_action(step.ref, item.action_id)
        Path(item.staging_path).write_bytes(b"# Native plan")
        launch = self.native_artifact(step.ref, item.action_id, item.prompt_path, "No final LF")
        target = (Path(self.home.name).resolve() / ".claude" / "projects" / launch.output_file.parent.parent.parent.name
                  / step.ref.session_segment / "subagents" / f"agent-{launch.handle}.jsonl")
        target.parent.mkdir(parents=True)
        launch.output_file.rename(target)
        launch.output_file.symlink_to(target)
        native.bind_native_launch(launch)
        captured = native.capture_native_return(launch, completion_observed=True)
        self.assertEqual(captured.type, "work_batch")
        foreign = target.parent.parent / "foreign" / target.name
        foreign.parent.mkdir()
        foreign.write_bytes(target.read_bytes())
        launch.output_file.unlink()
        target.unlink()
        self.assertEqual(native.capture_native_return(launch, completion_observed=True), captured)
        launch.output_file.symlink_to(foreign)
        with self.assertRaises(rw.WorkflowError):
            native.capture_native_return(launch, completion_observed=True)

    def test_native_binding_requires_claim_and_rejects_foreign_handle_owner(self) -> None:
        step = self.start()
        item = step.work_items[0]
        launch = self.native_artifact(step.ref, item.action_id, item.prompt_path)
        with self.assertRaises(rw.WorkflowError):
            native.bind_native_launch(launch)
        mt.claim_measure_action(step.ref, item.action_id)
        native.bind_native_launch(launch)
        before = self.state()
        other = dataclasses.replace(launch, handle="a000000000000000", output_file=launch.output_file.with_name("a000000000000000.output"))
        with self.assertRaises(rw.WorkflowError):
            native.bind_native_launch(other)
        foreign = dataclasses.replace(launch, ref=mt.MeasureRef("isolated", "foreign"))
        with self.assertRaises(rw.WorkflowError):
            native.capture_native_return(foreign, completion_observed=True)
        self.assertEqual(self.state(), before)

    def test_native_capture_never_uses_file_state_as_completion(self) -> None:
        step = self.start()
        item = step.work_items[0]
        launch = self.native_artifact(step.ref, item.action_id, item.prompt_path)
        with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("must not read a task artifact")):
            for call in (lambda: native.read_completed_return(launch, Path(item.prompt_path), completion_observed=False),
                         lambda: native.capture_native_return(launch, completion_observed=False)):
                with self.assertRaises(rw.WorkflowError) as caught:
                    call()
                self.assertEqual(caught.exception.code, "completion_not_observed")

    def test_native_artifact_rejects_incomplete_ambiguous_foreign_and_unsupported(self) -> None:
        step = self.start()
        item = step.work_items[0]
        mt.claim_measure_action(step.ref, item.action_id)
        launch = self.native_artifact(step.ref, item.action_id, item.prompt_path)
        native.bind_native_launch(launch)
        original = launch.output_file.read_bytes()
        records = [json.loads(line) for line in original.splitlines()]
        variants = [original[:-1], original + b"{unfinished", b"[]\n", original.splitlines(keepends=True)[0],
                    original + original.splitlines(keepends=True)[1]]
        for key, value in (("agentId", "a999999999999999"), ("sessionId", "foreign"),
                           ("cwd", "/foreign"), ("version", "unsupported")):
            changed = [{**record, key: value} for record in records]
            variants.append(b"".join(json.dumps(record).encode() + b"\n" for record in changed))
        changed = json.loads(json.dumps(records))
        changed[0]["message"]["content"] = "Read a foreign prompt and perform exactly the issued action."
        variants.append(b"".join(json.dumps(record).encode() + b"\n" for record in changed))
        changed = json.loads(json.dumps(records))
        changed[1]["message"]["content"][0]["input"] = {"message": ["not bytes"]}
        variants.append(b"".join(json.dumps(record).encode() + b"\n" for record in changed))
        before = self.state()
        for index, content in enumerate(variants):
            with self.subTest(variant=index), self.assertRaises(rw.WorkflowError):
                launch.output_file.write_bytes(content)
                native.capture_native_return(launch, completion_observed=True)
            self.assertEqual(self.state(), before)
        self.assertFalse(Path(item.staging_path).exists())

    def test_native_receipt_rejects_changed_prompt_symlink_and_replay_bytes(self) -> None:
        step = self.start()
        item = step.work_items[0]
        mt.claim_measure_action(step.ref, item.action_id)
        launch = self.native_artifact(step.ref, item.action_id, item.prompt_path)
        native.bind_native_launch(launch)
        prompt = Path(item.prompt_path)
        original = prompt.read_bytes()
        prompt.write_bytes(original + b"foreign addition")
        with self.assertRaises(rw.WorkflowError):
            native.capture_native_return(launch, completion_observed=True)
        prompt.write_bytes(original)
        moved = launch.output_file.with_suffix(".actual")
        launch.output_file.rename(moved)
        launch.output_file.symlink_to(moved)
        with self.assertRaises(rw.WorkflowError):
            native.capture_native_return(launch, completion_observed=True)
        launch.output_file.unlink()
        moved.rename(launch.output_file)
        Path(item.staging_path).write_bytes(b"# Native plan")
        native.capture_native_return(launch, completion_observed=True)
        native_dir = Path(item.prompt_path).parents[1] / "native-transport"
        next(native_dir.glob("*.return.txt")).write_bytes(b"changed replay bytes")
        with self.assertRaises(rw.WorkflowError):
            native.capture_native_return(launch, completion_observed=True)

    def default_stop(self, *, continued: bool = False) -> dict[str, object]:
        payload = {
            "hook_event_name": "Stop", "session_id": "isolated", "cwd": str(self.root),
            "transcript_path": str(self.root / "session-stream.jsonl"),
            "permission_mode": "default", "stop_hook_active": continued,
            "last_assistant_message": "STOP_PROBE_READY",
        }
        env = dict(os.environ)
        env.pop("CREW_VERBOSE", None)
        result = subprocess.run([sys.executable, str(Path(mt.__file__).parents[1] / "persistent-mode.py")],
            input=json.dumps(payload), cwd=self.root, env=env, capture_output=True, text=True, timeout=7)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def stop_next_argv(self, payload: dict[str, object]) -> list[str]:
        self.assertEqual(payload["decision"], "block")
        reason = payload["reason"]
        self.assertIsInstance(reason, str)
        lines = [line.removeprefix("Next: ") for line in reason.splitlines() if line.startswith("Next: ")]
        self.assertEqual(len(lines), 1, reason)
        self.assertNotIn("Waiting on seats? Just wait.", reason)
        return shlex.split(lines[0])

    def test_default_stop_issues_next_for_same_journal_owner_and_actions(self) -> None:
        step = self.start()
        for claimed in (False, True):
            if claimed:
                mt.claim_measure_action(step.ref, step.work_items[0].action_id)
            before = self.state()
            self.assertEqual(before["schema"], models.SCHEMA_VERSION)
            payload = self.default_stop(continued=claimed)
            argv = self.stop_next_argv(payload)
            self.assertEqual(argv, [str(Path(mt.__file__).parents[2] / "crew"), "measure-twice-next",
                "--session-segment", step.ref.session_segment, "--loop-instance-id", step.ref.loop_instance_id])
            after = self.state()
            self.assertIn(f"stop fires: {after['stop_fires']}/{after['max_stop_fires']}", payload["reason"])
            self.assertIn(f"fires={after['stop_fires']}/{after['max_stop_fires']}", payload["reason"])
            self.assertEqual(after["mt_workflow"], before["mt_workflow"])
            for key in ("started_at", "deadline_minutes", "max_stop_fires", "max_parked_fires", "loop_instance_id"):
                self.assertEqual(after[key], before[key])
            self.assertEqual(after["stop_fires"], before["stop_fires"] + 1)
            result = self.cli(*argv[1:])
            self.assertEqual(result.returncode, 0, result.stdout)
            resumed = json.loads(result.stdout)
            self.assertEqual(resumed["ref"]["loop_instance_id"], step.ref.loop_instance_id)
            self.assertEqual(resumed["ref"]["session_segment"], step.ref.session_segment)
            items = resumed["in_flight"] if claimed else resumed["work_items"]
            action_ids = items if claimed else [item["action_id"] for item in items]
            self.assertEqual(action_ids, [step.work_items[0].action_id])
            self.assertEqual(resumed["type"], "waiting" if claimed else "work_batch")

        review = self.draft(step)
        before = self.state()
        argv = self.stop_next_argv(self.default_stop(continued=True))
        result = self.cli(*argv[1:])
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual([item["action_id"] for item in json.loads(result.stdout)["work_items"]],
                         [item.action_id for item in review.work_items])
        self.assertEqual(self.state()["mt_workflow"], before["mt_workflow"])

    def test_default_stop_legacy_resume_and_finalization(self) -> None:
        for phase in ("drafting", "done"):
            with self.subTest(phase=phase):
                models.atomic_write_json(loop_state.resolve("mt", "isolated"), {
                    "schema": 3, "loop": "mt", "active": True, "session_id": "isolated",
                    "loop_instance_id": "legacy-stop", "task": "--seats opus preserved task", "phase": phase,
                    "stop_fires": 7, "revision_round": 2, "started_at": models.utc_now_iso(),
                    "last_verdict": "APPROVED" if phase == "done" else "",
                    "last_verdict_overrides": ["quorum-not-met"] if phase == "done" else []})
                before = self.state()
                argv = self.stop_next_argv(self.default_stop())
                self.assertEqual(argv, [str(Path(mt.__file__).parents[2] / "crew"), "measure-twice-resume",
                                       "--session-id", "isolated"])
                self.assertNotIn("mt_workflow", self.state())
                self.assertEqual(self.state()["stop_fires"], 8)
                self.assertEqual(self.state()["started_at"], before["started_at"])
                result = self.cli(*argv[1:])
                self.assertEqual(result.returncode, 0, result.stdout)
                resumed = json.loads(result.stdout)
                self.assertEqual(resumed["ref"]["loop_instance_id"], "legacy-stop")
                self.assertEqual(self.state()["revision_round"], 2)
                if phase == "drafting":
                    self.assertEqual(resumed["type"], "needs_input")
                    self.assertEqual(resumed["question"]["kind"], "legacy_work_not_running")
                else:
                    self.assertEqual(resumed["type"], "terminal")
                    self.assertEqual(resumed["outcome"]["overrides"], ["quorum-not-met"])
                self.assertFalse(resumed.get("work_items"))
                self.assertIsNone(mt.journal_from_dict(self.state()["mt_workflow"]).action)

    def test_default_stop_done_journal_guides_only_finalization(self) -> None:
        with mock.patch.object(loop_state, "deactivate", side_effect=OSError("crash after done")), self.assertRaises(OSError):
            self.panel(self.draft())
        before = self.state()
        self.assertEqual(before["phase"], "done")
        self.assertTrue(before["active"])
        argv = self.stop_next_argv(self.default_stop())
        self.assertEqual(argv, [str(Path(mt.__file__).parents[2] / "crew"), "measure-twice-next",
            "--session-segment", "isolated", "--loop-instance-id", before["loop_instance_id"]])
        self.assertEqual(self.state()["mt_workflow"], before["mt_workflow"])
        result = self.cli(*argv[1:])
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(json.loads(result.stdout)["outcome"]["status"], "approved")
        self.assertFalse(self.state()["active"])
        self.assertEqual(self.state()["mt_workflow"], before["mt_workflow"])
        self.assertEqual(self.state()["revision_round"], before["revision_round"])

    def test_issued_synthesis_guides_approved_capture_and_preserves_validator(self) -> None:
        review = self.draft()
        for item in review.work_items:
            rw.claim_review_action(rw.ClaimRequest(item.review_ref, item.action_id))
            transport.capture_review_return(item.review_ref, item.action_id, VALID)
        synthesis = mt.next_measure_twice(review.ref).work_items[0]
        prompt = Path(synthesis.prompt_path).read_text()
        self.assertIn("APPROVED means minor_only=false", prompt)
        self.assertIn("--minor-only is only valid with REVISE", prompt)
        rw.claim_review_action(rw.ClaimRequest(synthesis.review_ref, synthesis.action_id))
        returned = Path(synthesis.returned_path)
        returned.parent.mkdir(parents=True, exist_ok=True)
        returned.write_text("Approved with only minor findings. BLOCKING_CAUSES: 0")
        flags = ("--session-segment", synthesis.review_ref.session_segment,
            "--run-id", synthesis.review_ref.run_id, "--attempt-id", synthesis.review_ref.attempt_id,
            "--target-sha256", synthesis.review_ref.target_sha256, "--action-id", synthesis.action_id)
        workflow = self.root / ".crew" / "reviews" / "isolated" / synthesis.review_ref.run_id / "workflow.json"
        before = workflow.read_bytes()
        invalid = self.cli("review-capture", *flags, "-f", str(returned), "--verdict", "APPROVED", "--minor-only")
        self.assertEqual(invalid.returncode, 2, invalid.stdout)
        self.assertIn("invalid_submission", invalid.stdout + invalid.stderr)
        self.assertEqual(workflow.read_bytes(), before)
        valid = self.cli("review-capture", *flags, "-f", str(returned), "--verdict", "APPROVED")
        self.assertEqual(valid.returncode, 0, valid.stdout)
        self.assertEqual(json.loads(valid.stdout)["outcome"]["minor_only"], False)
        self.assertEqual(mt.next_measure_twice(review.ref).outcome["status"], "approved")

    def test_document_skips_interview_and_freezes_requirements(self) -> None:
        step = self.start()
        self.assertEqual(step.type, "work_batch")
        self.assertEqual(step.work_items[0].role, "crew:advisor")
        self.assertEqual(step.work_items[0].model, "inherit")
        self.assertEqual(mt.journal_from_dict(self.state()["mt_workflow"]).requirements.mode, "document")

    def test_pure_ordered_questions_and_answers_before_activation(self) -> None:
        request = mt.MeasureRequest("do a task $(do not execute) `text`", "isolated")
        question = mt.start_measure_twice(request).question
        self.assertEqual(question, mt.start_measure_twice(request).question)
        self.assertEqual(tuple(q[0] for q in question.questions), ("scope", "constraints", "acceptance"))
        self.assertFalse(loop_state.resolve("mt", "isolated").exists())
        ready = dataclasses.replace(request, requirements=mt.CapturedRequirements(question.question_id, ("a", "b", "c")))
        self.assertEqual(mt.start_measure_twice(ready).type, "work_batch")

    def test_wrong_answers_and_conflicting_sources(self) -> None:
        for request in (dataclasses.replace(self.request, requirements=mt.CapturedRequirements("wrong", ("a", "b", "c"))),
                        mt.MeasureRequest("a.md b.md", "isolated"),
                        mt.MeasureRequest("task", "isolated", mt.CapturedRequirements("wrong", ("a", "b", "c")))):
            with self.subTest(request=request), self.assertRaises(rw.WorkflowError) as caught:
                mt.start_measure_twice(request)
            self.assertEqual(caught.exception.code, "conflicting_requirements_sources")
            self.assertFalse(loop_state.resolve("mt", "isolated").exists())

    def test_unreadable_document_diagnostic(self) -> None:
        with self.assertRaises(rw.WorkflowError) as caught:
            mt.start_measure_twice(mt.MeasureRequest("missing.md", "isolated"))
        self.assertEqual(caught.exception.code, "requirements_source_unreadable")
        self.assertIn("missing.md", str(caught.exception))

    def test_embedded_document_is_task_text_and_quoted_spaces_resolve(self) -> None:
        self.assertEqual(mt.start_measure_twice(mt.MeasureRequest("implement according to requirements.md please", "isolated")).type, "needs_input")
        (self.root / "with spaces.md").write_text("requirements")
        self.assertEqual(mt.start_measure_twice(mt.MeasureRequest("--seats opus 'with spaces.md'", "isolated")).type, "work_batch")

    def test_option_parser_preserves_task_bytes_and_absent_selection(self) -> None:
        raw = "--panel quick --seats opus,sonnet task 'quoted'\n$(literal)\n"
        selected = mt.parse_arguments(raw)
        self.assertEqual(selected.task, "task 'quoted'\n$(literal)\n")
        self.assertEqual(selected.panel, "quick")
        self.assertIsNone(mt.parse_arguments("task").panel)
        for raw in ("--panel", "--panel --seats opus", "--panel quick --panel full task", "--bad task"):
            with self.subTest(raw=raw), self.assertRaises(rw.WorkflowError):
                mt.parse_arguments(raw)

    def test_active_identity_conflict_preserves_all_bytes(self) -> None:
        step = self.start()
        path = loop_state.resolve("mt", "isolated")
        before = path.read_bytes()
        with self.assertRaises(rw.WorkflowError) as caught:
            mt.start_measure_twice(dataclasses.replace(self.request, raw_arguments="--seats opus requirements.md"))
        self.assertEqual(caught.exception.code, "active_request_conflict")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(mt.start_measure_twice(self.request).ref, step.ref)

    def test_unsupported_planning_routes_do_not_activate(self) -> None:
        for host in ("cursor", "unknown"):
            with mock.patch.dict(os.environ, {"CREW_HOST": host}), self.subTest(host=host), self.assertRaises(rw.WorkflowError) as caught:
                self.start()
            self.assertEqual(caught.exception.code, "unsupported_planning_host")
            self.assertFalse(loop_state.resolve("mt", "isolated").exists())

    def test_once_only_claim_and_waiting(self) -> None:
        step = self.start()
        item = step.work_items[0]
        self.assertEqual(mt.claim_measure_action(step.ref, item.action_id)["authorization"], "spawn")
        self.assertEqual(mt.claim_measure_action(step.ref, item.action_id)["authorization"], "do_not_spawn")
        self.assertEqual(mt.next_measure_twice(step.ref).in_flight, (item.action_id,))

    def test_lost_claim_requires_confirmation(self) -> None:
        step = self.start()
        item = step.work_items[0]
        mt.claim_measure_action(step.ref, item.action_id)
        with self.assertRaises(rw.WorkflowError):
            mt.recover_measure_action(mt.MeasureRecovery(step.ref, item.action_id, "timeout"))
        wait = mt.recover_measure_action(mt.MeasureRecovery(step.ref, item.action_id, "not_running"))
        self.assertTrue(self.state()["awaiting_input"])
        retry = mt.decide_measure_twice(step.ref, mt.MeasureDecision(step.ref, wait.question.question_id, "retry_advisor"))
        self.assertNotEqual(retry.work_items[0].staging_path, item.staging_path)

    def test_approve_and_minor_only_finalize(self) -> None:
        for verdict, minor in (("APPROVED", False), ("REVISE", True)):
            with self.subTest(verdict=verdict):
                self.request = dataclasses.replace(self.request, session_id=verdict.lower())
                completed = self.panel(self.draft(), verdict, minor)
                self.assertEqual(completed.type, "terminal")
                self.assertEqual(completed.outcome["status"], "approved")
                self.assertEqual(mt.next_measure_twice(completed.ref), completed)

    def test_blocking_revision_and_reject_replan(self) -> None:
        for verdict, stage in (("REVISE", "revision"), ("REJECT", "replanning")):
            self.request = dataclasses.replace(self.request, session_id=verdict.lower())
            step = self.panel(self.draft(), verdict)
            self.assertEqual(step.work_items[0].kind, "advisor")
            data = loop_state.read(loop_state.resolve("mt", verdict.lower()))
            self.assertEqual(data["revision_round"], 1)
            self.assertEqual(mt.journal_from_dict(data["mt_workflow"]).stage, stage)

    def test_all_failed_new_generation_and_second_failure_exit(self) -> None:
        panel = self.draft()
        next_panel = self.panel(panel, failed=2)
        self.assertEqual(next_panel.work_items[0].kind, "reviewer")
        self.assertNotEqual(next_panel.work_items[0].review_ref.run_id, panel.work_items[0].review_ref.run_id)
        terminal = self.panel(next_panel, failed=2)
        self.assertEqual(terminal.outcome["status"], "review_failed")
        self.assertEqual(self.state()["consecutive_review_failures"], 2)

    def test_synthesis_only_retry_preserves_successes(self) -> None:
        panel = self.draft()
        wait = self.panel(panel, synthesis_fail=True)
        self.assertEqual(wait.question.kind, "synthesis_retry")
        again = mt.next_measure_twice(wait.ref)
        self.assertEqual(again.question, wait.question)
        retry = mt.decide_measure_twice(wait.ref, mt.MeasureDecision(wait.ref, wait.question.question_id, "retry_synthesis"))
        self.assertEqual([i.kind for i in retry.work_items], ["synthesis"])
        self.assertEqual(retry.work_items[0].review_ref.attempt_id, "attempt-0002")
        self.assertEqual(self.state()["consecutive_review_failures"], 0)

    def test_partial_completion_advisory_and_exact_human_force(self) -> None:
        wait = self.panel(self.draft(), failed=1)
        self.assertEqual(wait.question.kind, "completion_advisory")
        self.assertEqual(self.state()["last_verdict"], "")
        with self.assertRaises(rw.WorkflowError):
            mt.decide_measure_twice(wait.ref, mt.MeasureDecision(wait.ref, "stale", "force", "force"))
        completed = mt.decide_measure_twice(wait.ref, mt.MeasureDecision(wait.ref, wait.question.question_id, "force", "force"))
        self.assertEqual(completed.outcome["overrides"], ["quorum-not-met"])
        self.assertIn("recorded over", completed.display)

    def test_quorum_force_cannot_authorize_new_target_drift(self) -> None:
        wait = self.panel(self.draft(), failed=1)
        self.assertEqual(wait.question.advisories, ("quorum-not-met",))
        decision = mt.MeasureDecision(wait.ref, wait.question.question_id, "force", "force")
        before = self.state()
        Path(before["plan_file"]).write_bytes(b"# Newly changed target\n")
        reviews = {p: p.read_bytes() for p in (self.root / ".crew" / "reviews").rglob("*") if p.is_file()}
        changed = mt.decide_measure_twice(wait.ref, decision)
        self.assertEqual(changed.type, "needs_input")
        self.assertEqual(changed.question.advisories, ("quorum-not-met", "target-drift"))
        self.assertNotEqual(changed.question.question_id, wait.question.question_id)
        self.assertIn(mt.sha256(b"# Newly changed target\n"), changed.question.text)
        after = self.state()
        self.assertTrue(after["active"])
        self.assertEqual(after["phase"], "reviewing")
        self.assertEqual(after["last_verdict"], "")
        self.assertEqual(after["mt_workflow"]["applied_outcomes"], before["mt_workflow"]["applied_outcomes"])
        self.assertEqual(after["mt_workflow"]["review_ref"], before["mt_workflow"]["review_ref"])
        self.assertEqual(changed.question.outcome_sha256, wait.question.outcome_sha256)
        self.assertEqual(mt.next_measure_twice(wait.ref), changed)
        self.assertEqual({p: p.read_bytes() for p in (self.root / ".crew" / "reviews").rglob("*") if p.is_file()}, reviews)

    def test_changed_drifted_target_rebinds_and_public_cli_refuses_stale_replay(self) -> None:
        panel = self.draft()
        plan = Path(self.state()["plan_file"])
        plan.write_bytes(b"# First drift\n")
        wait = self.panel(panel, failed=1)
        plan.write_bytes(b"# Second drift\n")
        flags = ("--session-segment", wait.ref.session_segment, "--loop-instance-id", wait.ref.loop_instance_id,
                 "--question-id", wait.question.question_id, "--kind", "force", "--confirmation", "force")
        result = self.cli("measure-twice-decide", *flags)
        self.assertEqual(result.returncode, 0, result.stderr)
        question = json.loads(result.stdout)["question"]
        self.assertEqual(question["advisories"], list(wait.question.advisories))
        self.assertNotEqual(question["question_id"], wait.question.question_id)
        self.assertIn(mt.sha256(b"# Second drift\n"), question["text"])
        before = self.filesystem_snapshot()
        replay = self.cli("measure-twice-decide", *flags)
        self.assertEqual(replay.returncode, 2, replay.stderr)
        self.assertEqual(json.loads(replay.stdout)["code"], "stale_question")
        self.assertEqual(self.filesystem_snapshot(), before)
        self.assertEqual(self.state()["last_verdict"], "")

    def test_target_reversion_does_not_revive_an_older_force_question(self) -> None:
        wait = self.panel(self.draft(), failed=1)
        plan = Path(self.state()["plan_file"])
        original = plan.read_bytes()
        old = mt.MeasureDecision(wait.ref, wait.question.question_id, "force", "force")
        plan.write_bytes(b"# Intermediate drift\n")
        changed = mt.decide_measure_twice(wait.ref, old)
        plan.write_bytes(original)
        reverted = mt.decide_measure_twice(wait.ref,
            dataclasses.replace(old, question_id=changed.question.question_id))
        self.assertEqual(reverted.type, "needs_input")
        self.assertEqual(reverted.question.advisories, wait.question.advisories)
        self.assertNotIn(reverted.question.question_id, (wait.question.question_id, changed.question.question_id))
        before = self.filesystem_snapshot()
        with self.assertRaises(rw.WorkflowError) as stale:
            mt.decide_measure_twice(wait.ref, old)
        self.assertEqual(stale.exception.code, "stale_question")
        self.assertEqual(self.filesystem_snapshot(), before)

    def test_force_rebinds_changed_resolution_and_cleared_advisories(self) -> None:
        for missing in (True, False):
            with self.subTest(missing=missing):
                panel = self.draft()
                plan = Path(self.state()["plan_file"])
                original = plan.read_bytes()
                if not missing:
                    plan.write_bytes(b"# Previously disclosed drift\n")
                wait = self.panel(panel, failed=1 if missing else 0)
                if missing:
                    plan.unlink()
                else:
                    plan.write_bytes(original)
                changed = mt.decide_measure_twice(wait.ref,
                    mt.MeasureDecision(wait.ref, wait.question.question_id, "force", "force"))
                self.assertEqual(changed.type, "needs_input")
                self.assertEqual(changed.question.advisories,
                    ("quorum-not-met", "target-no-longer-resolves") if missing else ())
                self.assertNotEqual(changed.question.question_id, wait.question.question_id)
                self.assertEqual(self.state()["mt_workflow"]["applied_outcomes"], [])
                completed = mt.decide_measure_twice(changed.ref,
                    mt.MeasureDecision(changed.ref, changed.question.question_id, "force", "force"))
                self.assertEqual(completed.outcome["status"], "approved")
                self.assertEqual(completed.outcome["overrides"], list(changed.question.advisories))
                loop_state.resolve("mt", "isolated").unlink()

    def test_unchanged_disclosed_drift_allows_explicit_cli_force_once(self) -> None:
        panel = self.draft()
        Path(self.state()["plan_file"]).write_bytes(b"# Disclosed drift\n")
        wait = self.panel(panel, failed=1)
        before = self.state()["mt_workflow"]
        result = self.cli("measure-twice-decide", "--session-segment", wait.ref.session_segment,
            "--loop-instance-id", wait.ref.loop_instance_id, "--question-id", wait.question.question_id,
            "--kind", "force", "--confirmation", "force")
        self.assertEqual(result.returncode, 0, result.stderr)
        terminal = json.loads(result.stdout)
        self.assertEqual(terminal["type"], "terminal")
        self.assertEqual(terminal["outcome"]["overrides"], ["quorum-not-met", "target-drift"])
        after = self.state()["mt_workflow"]
        self.assertEqual(after["accepted_actions"], before["accepted_actions"])
        self.assertEqual(after["review_generation"], before["review_generation"])
        self.assertEqual(after["applied_outcomes"], [wait.question.outcome_sha256])
        self.assertEqual(mt.next_measure_twice(wait.ref).outcome, terminal["outcome"])

    def test_pointer_isolation_and_standalone_next_refusal(self) -> None:
        panel = self.draft()
        directory = self.root / ".crew" / "reviews" / "isolated"
        self.assertFalse((directory / "current-run.json").exists())
        self.assertFalse((directory / "current-standalone-review.json").exists())
        standalone = rw.start_review(rw.ReviewRequest(str(self.root / "requirements.md"), seats="opus", session_id="isolated"))
        pointer = (directory / "current-standalone-review.json").read_bytes()
        (directory / "current-run.json").write_text('{"run_id":"run-111111111111"}')
        with self.assertRaises(rw.WorkflowError) as caught:
            rw.next_review(panel.work_items[0].review_ref)
        self.assertEqual(caught.exception.code, "loop_owned_review")
        self.assertEqual(self.panel(panel).outcome["status"], "approved")
        self.assertEqual((directory / "current-standalone-review.json").read_bytes(), pointer)
        self.assertEqual(rw.next_review(standalone.ref).ref, standalone.ref)

    def test_cancel_invalidates_late_review_and_advisor(self) -> None:
        step = self.start()
        item = step.work_items[0]
        mt.claim_measure_action(step.ref, item.action_id)
        mt.cancel_measure_twice(step.ref, "cancel")
        Path(item.staging_path).write_bytes(b"late old write")
        with self.assertRaises(rw.WorkflowError):
            transport.capture_measure_return(step.ref, item.action_id, b"late")
        self.assertFalse(self.state()["active"])

    def test_cancel_after_review_claim_refuses_acceptance(self) -> None:
        step = self.draft()
        item = step.work_items[0]
        rw.claim_review_action(rw.ClaimRequest(item.review_ref, item.action_id))
        mt.cancel_measure_twice(step.ref, "cancel")
        with self.assertRaises(rw.WorkflowError):
            transport.capture_review_return(item.review_ref, item.action_id, VALID)

    def test_promotion_replay_after_pending_and_canonical_write(self) -> None:
        step = self.start()
        item = step.work_items[0]
        mt.claim_measure_action(step.ref, item.action_id)
        Path(item.staging_path).write_bytes(b"# Sealed plan")
        result = mt.MeasureResult(step.ref, item.action_id, "ok", mt.sha256(b"# Sealed plan"), mt.sha256(b"done"))
        with mock.patch.object(mt, "next_measure_twice", side_effect=OSError("crash after pending")), self.assertRaises(OSError):
            mt.submit_measure_action(mt.MeasureSubmission(result))
        Path(item.staging_path).write_bytes(b"late staging mutation")
        journal = mt.journal_from_dict(self.state()["mt_workflow"])
        Path(journal.action.canonical_path).write_bytes(b"# Sealed plan")
        panel = mt.next_measure_twice(step.ref)
        self.assertEqual(panel.work_items[0].kind, "reviewer")
        self.assertEqual(Path(self.state()["plan_file"]).read_bytes(), b"# Sealed plan")
        self.assertEqual(mt.submit_measure_action(mt.MeasureSubmission(result)).ref, step.ref)

    def test_crash_before_bind_keeps_frozen_checkpoint_across_config_change(self) -> None:
        step = self.start()
        item = step.work_items[0]
        mt.claim_measure_action(step.ref, item.action_id)
        Path(item.staging_path).write_bytes(b"# Plan")
        with mock.patch.object(rw, "_start_loop_review_under_owner_lock", side_effect=OSError("crash before bind")), self.assertRaises(OSError):
            transport.capture_measure_return(step.ref, item.action_id, b"done")
        before = mt.journal_from_dict(self.state()["mt_workflow"]).pending_review_inputs
        (self.root / ".crew" / "config.toml").write_text('[seats.opus]\nmodel="different-model"\n')
        config._reset_cache_for_tests()
        resumed = mt.next_measure_twice(step.ref)
        self.assertEqual(resumed.work_items[0].review_ref, before.ref)
        self.assertEqual([i.model for i in resumed.work_items], ["opus", "sonnet"])

    def test_receipt_application_crash_replays_once(self) -> None:
        step = self.draft()
        for item in step.work_items:
            rw.claim_review_action(rw.ClaimRequest(item.review_ref, item.action_id))
            transport.capture_review_return(item.review_ref, item.action_id, VALID)
        synthesis = mt.next_measure_twice(step.ref).work_items[0]
        rw.claim_review_action(rw.ClaimRequest(synthesis.review_ref, synthesis.action_id))
        transport.capture_review_return(synthesis.review_ref, synthesis.action_id, b"revise", judgment=transport.SynthesisJudgment("REVISE"))
        with mock.patch.object(mt, "_apply_outcome", side_effect=OSError("crash before apply")), self.assertRaises(OSError):
            mt.next_measure_twice(step.ref)
        after = mt.next_measure_twice(step.ref)
        self.assertEqual(self.state()["revision_round"], 1)
        self.assertEqual(mt.next_measure_twice(step.ref).work_items, after.work_items)
        self.assertEqual(self.state()["revision_round"], 1)

    def test_schema4_round_trip_and_schema3_loader_refuses_touch(self) -> None:
        self.start()
        path = loop_state.resolve("mt", "isolated")
        self.assertIsNotNone(models.LoopState.load(path).mt_workflow)
        before = path.read_bytes()
        with mock.patch.object(models, "SCHEMA_VERSION", 3):
            self.assertEqual(models.read_state_json(path)[1], models.LOAD_FUTURE_SCHEMA)
            called = []
            models.update_state_json(path, lambda data: called.append(data))
            self.assertFalse(called)
        self.assertEqual(path.read_bytes(), before)

    def test_legacy_quiescence_requires_bound_confirmation_preserves_budget(self) -> None:
        path = loop_state.resolve("mt", "isolated")
        models.atomic_write_json(path, {"schema": 3, "loop": "mt", "active": True, "session_id": "isolated",
            "task": "--seats opus requirements.md", "phase": "drafting", "stop_fires": 9,
            "loop_instance_id": "legacy", "plan_file": str(self.root / "absent.md")})
        wait = self.start()
        self.assertEqual(wait.question.kind, "legacy_work_not_running")
        self.assertEqual(self.state()["stop_fires"], 9)
        with self.assertRaises(rw.WorkflowError):
            mt.decide_measure_twice(wait.ref, mt.MeasureDecision(wait.ref, wait.question.question_id, "legacy_work_not_running", "not_running"))
        ready = mt.decide_measure_twice(wait.ref, mt.MeasureDecision(wait.ref, wait.question.question_id,
            "legacy_work_not_running", "not_running", "drafting", ""))
        self.assertEqual(ready.work_items[0].kind, "advisor")
        self.assertEqual(self.state()["stop_fires"], 9)

    def test_direct_native_runtime_launches_entire_batch_before_completion(self) -> None:
        step = self.draft()
        launches: list[str] = []
        class Runtime:
            def launch(self, item: mt.MeasureWorkItem) -> str:
                self.assert_role(item)
                launches.append(item.action_id)
                return item.action_id
            def assert_role(self, item: mt.MeasureWorkItem) -> None:
                if item.role == "crew:scribe":
                    raise AssertionError("direct runtime requested a scribe")
            def completions(self, handles: tuple[str, ...]):
                if len(launches) != 2:
                    raise AssertionError("batch did not overlap")
                for handle in handles:
                    yield transport.Completion(handle, VALID)
            def cancel(self, handle: str) -> bool:
                return True
        completed = transport.run_measure_batch(step, Runtime())
        self.assertEqual([item.kind for item in completed.work_items], ["synthesis"])
        self.assertEqual(len(launches), 2)

    def test_review_capture_before_submit_crash_is_immutable_and_public_replayable(self) -> None:
        panel = self.draft()
        item = panel.work_items[0]
        rw.claim_review_action(rw.ClaimRequest(item.review_ref, item.action_id))
        with mock.patch.object(rw, "submit_review", side_effect=OSError("crash before submit")), self.assertRaises(OSError):
            transport.capture_review_return(item.review_ref, item.action_id, VALID)
        before = self.filesystem_snapshot()
        for content, kwargs in ((VALID + b"changed", {}),
                                (VALID, {"status": "failed", "diagnostic": "outage"}),
                                (VALID, {"capture_path": item.return_transport["primary"]["ingress_path"]})):
            with self.subTest(kwargs=kwargs), self.assertRaises(rw.WorkflowError) as conflict:
                transport.capture_review_return(item.review_ref, item.action_id, content, **kwargs)
            self.assertEqual(conflict.exception.code, "conflict")
            self.assertEqual(self.filesystem_snapshot(), before)
        returned = item.return_transport["fallback"]["ingress_path"]
        replay = self.cli("review-capture", *self.review_flags(item.review_ref), "--action-id", item.action_id, "-f", returned)
        self.assertEqual(replay.returncode, 0, replay.stdout + replay.stderr)
        self.assertEqual(Path(returned).read_bytes(), VALID)
        self.assertFalse(Path(item.submission_path).exists())
        self.assertEqual([work.action_id for work in mt.next_measure_twice(panel.ref).work_items], [panel.work_items[1].action_id])
        again = self.cli("review-capture", *self.review_flags(item.review_ref), "--action-id", item.action_id, "-f", returned)
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)

    def test_standalone_and_historical_capture_receipts_replay_without_rewriting(self) -> None:
        batch = rw.start_review(rw.ReviewRequest("requirements.md", seats="opus", session_id="standalone"))
        item = batch.work_items[0]
        rw.claim_review_action(rw.ClaimRequest(batch.ref, item.action_id))
        with mock.patch.object(rw, "submit_review", side_effect=OSError("crash before submit")), self.assertRaises(OSError):
            transport.capture_review_return(batch.ref, item.action_id, VALID)
        run = self.root / ".crew" / "reviews" / "standalone" / batch.ref.run_id
        receipt = next((run / "attempts" / batch.ref.attempt_id / "transport-receipts").glob("*.json"))
        saved = json.loads(receipt.read_bytes())
        saved.pop("capture_path")
        receipt.write_bytes(mt._canonical(saved))
        historical = receipt.read_bytes()
        result = self.cli("review-capture", *self.review_flags(batch.ref), "--action-id", item.action_id,
                          "-f", item.return_transport["fallback"]["ingress_path"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(receipt.read_bytes(), historical)
        self.assertEqual(rw.next_review(batch.ref).work_items[0].kind, "synthesis")
        self.assertFalse((self.root / ".crew" / "measure-twice-state-isolated.json").exists())
        step = self.start()
        advisor = step.work_items[0]
        mt.claim_measure_action(step.ref, advisor.action_id)
        Path(advisor.staging_path).write_bytes(b"# Historical retained plan")
        with mock.patch.object(mt, "submit_measure_action", side_effect=OSError("crash before submit")), self.assertRaises(OSError):
            transport.capture_measure_return(step.ref, advisor.action_id, b"observed")
        root = Path(advisor.prompt_path).parents[1]
        receipt = root / "transport-receipts" / f"{advisor.action_id}.json"
        saved = json.loads(receipt.read_bytes())
        saved["capture"].pop("capture_path")
        receipt.write_bytes(mt._canonical(saved))
        historical = receipt.read_bytes()
        Path(advisor.staging_path).unlink()
        transport.capture_measure_return(step.ref, advisor.action_id, b"observed")
        self.assertEqual(receipt.read_bytes(), historical)
        self.assertEqual(Path(self.state()["plan_file"]).read_bytes(), b"# Historical retained plan")

    def test_review_capture_validates_typed_observation_before_any_write(self) -> None:
        panel = self.draft()
        item = panel.work_items[0]
        rw.claim_review_action(rw.ClaimRequest(item.review_ref, item.action_id))
        before = self.filesystem_snapshot()
        for kwargs in ({"status": "unknown"}, {"diagnostic": 7}, {"diagnostic": "unexpected success diagnostic"},
                       {"judgment": transport.SynthesisJudgment("APPROVED")},
                       {"status": "failed"}, {"status": "failed", "diagnostic": "failure", "judgment": transport.SynthesisJudgment("REVISE")}):
            with self.subTest(kwargs=kwargs), self.assertRaises(rw.WorkflowError) as refused:
                transport.capture_review_return(item.review_ref, item.action_id, VALID, **kwargs)
            self.assertEqual(refused.exception.code, "invalid_submission")
            self.assertEqual(self.filesystem_snapshot(), before)
        transport.capture_review_return(item.review_ref, item.action_id, VALID)

    def test_synthesis_capture_retains_judgment_and_direct_submit_cannot_replace_it(self) -> None:
        panel = self.draft()
        for item in panel.work_items:
            rw.claim_review_action(rw.ClaimRequest(item.review_ref, item.action_id))
            transport.capture_review_return(item.review_ref, item.action_id, VALID)
        item = mt.next_measure_twice(panel.ref).work_items[0]
        rw.claim_review_action(rw.ClaimRequest(item.review_ref, item.action_id))
        with mock.patch.object(rw, "submit_review", side_effect=OSError("crash before submit")), self.assertRaises(OSError):
            transport.capture_review_return(item.review_ref, item.action_id, b"synthesis", judgment=transport.SynthesisJudgment("APPROVED"))
        before = self.filesystem_snapshot()
        for judgment in (transport.SynthesisJudgment("REVISE"), transport.SynthesisJudgment("REVISE", True)):
            with self.subTest(judgment=judgment), self.assertRaises(rw.WorkflowError) as conflict:
                transport.capture_review_return(item.review_ref, item.action_id, b"synthesis", judgment=judgment)
            self.assertEqual(conflict.exception.code, "conflict")
            self.assertEqual(self.filesystem_snapshot(), before)
        submission = Path(item.submission_path)
        original = submission.read_bytes()
        changed = json.loads(original)
        changed["judgment"] = {"verdict": "REVISE", "minor_only": False}
        submission.write_bytes(mt._canonical(changed))
        result = self.cli("review-submit", "-f", str(submission), "--consume")
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertEqual(json.loads(result.stdout)["code"], "conflict")
        submission.write_bytes(original)
        replay = self.cli("review-capture", *self.review_flags(item.review_ref), "--action-id", item.action_id,
                          "-f", item.returned_path, "--verdict", "APPROVED")
        self.assertEqual(replay.returncode, 0, replay.stdout + replay.stderr)
        self.assertEqual(mt.next_measure_twice(panel.ref).outcome["status"], "approved")

    def test_failed_review_capture_retains_raw_bytes_and_diagnostic_before_submit(self) -> None:
        panel = self.draft()
        item = panel.work_items[0]
        rw.claim_review_action(rw.ClaimRequest(item.review_ref, item.action_id))
        raw = b"failed provider return\x00\xff"
        with mock.patch.object(rw, "submit_review", side_effect=OSError("crash before submit")), self.assertRaises(OSError):
            transport.capture_review_return(item.review_ref, item.action_id, raw, status="failed", diagnostic="original failure")
        before = self.filesystem_snapshot()
        for content, status, diagnostic in ((raw+b"changed", "failed", "original failure"),
                                            (raw, "failed", "changed diagnostic"), (raw, "timeout", "original failure")):
            with self.subTest(status=status, diagnostic=diagnostic), self.assertRaises(rw.WorkflowError) as conflict:
                transport.capture_review_return(item.review_ref, item.action_id, content, status=status, diagnostic=diagnostic)
            self.assertEqual(conflict.exception.code, "conflict")
            self.assertEqual(self.filesystem_snapshot(), before)
        transport.capture_review_return(item.review_ref, item.action_id, raw, status="failed", diagnostic="original failure")
        self.assertEqual(Path(item.return_transport["fallback"]["ingress_path"]).read_bytes(), raw)

    def test_advisor_capture_binds_path_and_validates_without_overwriting_envelope(self) -> None:
        step = self.start()
        item = step.work_items[0]
        mt.claim_measure_action(step.ref, item.action_id)
        Path(item.staging_path).write_bytes(b"# Plan")
        before = self.filesystem_snapshot()
        for kwargs in ({"status": "unknown"}, {"diagnostic": 7}):
            with self.subTest(kwargs=kwargs), self.assertRaises(rw.WorkflowError):
                transport.capture_measure_return(step.ref, item.action_id, b"observed", **kwargs)
            self.assertEqual(self.filesystem_snapshot(), before)
        Path(item.staging_path).unlink()
        before = self.filesystem_snapshot()
        with self.assertRaises(rw.WorkflowError):
            transport.capture_measure_return(step.ref, item.action_id, b"observed", diagnostic=7)
        self.assertEqual(self.filesystem_snapshot(), before)
        Path(item.staging_path).write_bytes(b"# Plan")
        submission = Path(item.submission_path)
        submission.parent.mkdir(parents=True, exist_ok=True)
        submission.write_bytes(b"existing conflicting envelope")
        before = self.filesystem_snapshot()
        with self.assertRaises(rw.WorkflowError) as conflict:
            transport.capture_measure_return(step.ref, item.action_id, b"observed")
        self.assertEqual(conflict.exception.code, "conflict")
        self.assertEqual(self.filesystem_snapshot(), before)
        submission.unlink()
        with mock.patch.object(mt, "submit_measure_action", side_effect=OSError("crash before submit")), self.assertRaises(OSError):
            transport.capture_measure_return(step.ref, item.action_id, b"observed")
        before = self.filesystem_snapshot()
        with self.assertRaises(rw.WorkflowError) as conflict:
            transport.capture_measure_return(step.ref, item.action_id, b"observed", capture_path=item.returned_path)
        self.assertEqual(conflict.exception.code, "conflict")
        self.assertEqual(self.filesystem_snapshot(), before)
        transport.capture_measure_return(step.ref, item.action_id, b"observed")

    def test_capture_replay_without_consumed_submission_and_changed_bytes_conflict(self) -> None:
        step = self.draft()
        item = step.work_items[0]
        rw.claim_review_action(rw.ClaimRequest(item.review_ref, item.action_id))
        transport.capture_review_return(item.review_ref, item.action_id, VALID)
        self.assertFalse(Path(item.review_item.submission_path).exists())
        transport.capture_review_return(item.review_ref, item.action_id, VALID)
        with self.assertRaises(rw.WorkflowError):
            transport.capture_review_return(item.review_ref, item.action_id, VALID + b"changed")

    def test_expired_bound_prevents_paid_work_and_terminal_start_does_not_restart(self) -> None:
        step = self.start()
        loop_state.mutate(loop_state.resolve("mt", "isolated"), lambda data: {**data, "stop_fires": 150})
        with self.assertRaises(rw.WorkflowError):
            mt.claim_measure_action(step.ref, step.work_items[0].action_id)
        terminal = mt.next_measure_twice(step.ref)
        self.assertEqual(terminal.outcome["status"], "force_exit")
        self.assertEqual(mt.start_measure_twice(self.request).ref, step.ref)

    def test_future_schema_refuses_without_mutation(self) -> None:
        path = loop_state.resolve("mt", "isolated")
        models.atomic_write_json(path, {"schema": 999, "active": True})
        before = path.read_bytes()
        with self.assertRaises(loop_state.LoopStateError):
            self.start()
        self.assertEqual(path.read_bytes(), before)


    def cli(self, *arguments: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(Path(mt.__file__).parents[2] / "crew"), *arguments],
            cwd=cwd or self.root, env=dict(os.environ), capture_output=True, text=True, timeout=15)

    def legacy(self, phase: str = "drafting", task: str = "--seats opus preserved task",
               verdict: str = "", content: bytes | None = None) -> mt.MeasureStep:
        plan = self.root / "old-plan.md"
        if content is not None:
            plan.write_bytes(content)
        models.atomic_write_json(loop_state.resolve("mt", "isolated"), {
            "schema": 3, "loop": "mt", "active": True, "session_id": "isolated", "task": task,
            "phase": phase, "stop_fires": 7, "parked_fires": 3, "revision_round": 2,
            "consecutive_review_failures": 1, "loop_instance_id": "legacy", "run_id": "",
            "plan_file": str(plan), "last_verdict": verdict, "awaiting_input": True,
            "last_verdict_overrides": ["quorum-not-met"] if phase == "done" else []})
        return self.start()

    def confirm_legacy(self, wait: mt.MeasureStep, **completion: str) -> mt.MeasureStep:
        return mt.decide_measure_twice(wait.ref, mt.MeasureDecision(wait.ref, wait.question.question_id,
            "legacy_work_not_running", "not_running", wait.question.retained_phase,
            wait.question.retained_run_id, **completion))

    def test_cli_request_grammar_consume_and_divergent_cwd(self) -> None:
        spill = self.root / "request.json"
        raw = "--seats opus requirements.md"
        spill.write_text(json.dumps({"schema": 1, "raw_arguments": raw, "requirements": None}))
        other = self.root / "different cwd"
        other.mkdir()
        result = self.cli("measure-twice", "-f", str(spill), "--session-id", "isolated", "--consume", cwd=other)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        step = json.loads(result.stdout)
        self.assertEqual(step["type"], "work_batch")
        self.assertFalse(spill.exists())
        self.assertEqual(self.state()["task"], raw)
        item = step["work_items"][0]
        flags = ("--session-segment", "isolated", "--loop-instance-id", step["ref"]["loop_instance_id"])
        claim = self.cli("measure-twice-claim", *flags, "--action-id", item["action_id"], cwd=other)
        self.assertEqual(json.loads(claim.stdout)["authorization"], "spawn")
        Path(item["staging_path"]).write_bytes(b"# CLI plan")
        returned = Path(item["returned_path"])
        returned.parent.mkdir(parents=True, exist_ok=True)
        returned.write_bytes(b"plan written")
        captured = self.cli("measure-twice-capture", *flags, "--action-id", item["action_id"], "-f", str(returned), cwd=other)
        self.assertEqual(captured.returncode, 0, captured.stdout)
        self.assertEqual(json.loads(captured.stdout)["work_items"][0]["kind"], "reviewer")
        for changed in ({"schema": 1, "raw_arguments": "task", "requirements": {"question_id": "a" * 64, "answers": ["a"]}},
                        {"schema": True, "raw_arguments": "task", "requirements": None},
                        {"schema": 1, "raw_arguments": "task", "requirements": None, "extra": 1}):
            with self.subTest(changed=changed), self.assertRaises(rw.WorkflowError):
                mt.request_from_dict(changed, "isolated")

    def test_task_spill_preserves_shell_characters_and_crlf(self) -> None:
        raw = "--seats opus task 'space' `literal` $(touch DO-NOT-CREATE)\r\n"
        question = mt.interview_question(raw)
        spill = self.root / "request.json"
        spill.write_text(json.dumps({"schema": 1, "raw_arguments": raw,
            "requirements": {"question_id": question.question_id, "answers": ["scope", "constraints", "checks"]}}))
        result = self.cli("measure-twice", "-f", str(spill), "--session-id", "isolated")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.state()["task"], raw)
        self.assertFalse((self.root / "DO-NOT-CREATE").exists())

    def test_legacy_partial_or_missing_plan_never_proves_quiescence(self) -> None:
        for phase, content in (("drafting", None), ("drafting", b"partial write"), ("reviewing", b"partial write")):
            with self.subTest(phase=phase, content=content):
                wait = self.legacy(phase, content=content)
                self.assertEqual(wait.type, "needs_input")
                before = self.state()
                self.assertEqual(before["revision_round"], 2)
                self.assertEqual(before["stop_fires"], 7)
                self.assertIsNone(mt.journal_from_dict(before["mt_workflow"]).action)
                self.assertEqual(mt.next_measure_twice(wait.ref).question, wait.question)
                ready = self.confirm_legacy(wait)
                if content is None:
                    self.assertEqual(ready.work_items[0].kind, "advisor")
                else:
                    self.assertEqual(ready.work_items[0].kind, "reviewer")
                loop_state.resolve("mt", "isolated").unlink()
                (self.root / "old-plan.md").unlink(missing_ok=True)
                # Each legacy fixture is a separate lifetime, so remove only its test artifacts.
                shutil.rmtree(self.root / ".crew" / "plans", ignore_errors=True)
                shutil.rmtree(self.root / ".crew" / "reviews", ignore_errors=True)

    def test_legacy_review_missing_plan_and_prior_verdict_need_human_recovery(self) -> None:
        for phase, verdict in (("reviewing", ""), ("drafting", "REVISE"), ("drafting", "REJECT")):
            wait = self.legacy(phase, verdict=verdict)
            recovered = self.confirm_legacy(wait)
            self.assertEqual(recovered.question.kind, "legacy_plan_recovery")
            self.assertEqual(self.state()["revision_round"], 2)
            loop_state.resolve("mt", "isolated").unlink()

    def test_legacy_completed_revision_or_replan_attestation_avoids_advisor(self) -> None:
        for verdict, completed in (("REVISE", "revision"), ("REJECT", "replan")):
            wait = self.legacy(verdict=verdict, content=b"# Already revised")
            step = self.confirm_legacy(wait, completed_action=completed, plan_sha256=mt.sha256(b"# Already revised"))
            self.assertEqual(step.work_items[0].kind, "reviewer")
            self.assertEqual(self.state()["revision_round"], 2)
            loop_state.resolve("mt", "isolated").unlink()
            shutil.rmtree(self.root / ".crew" / "plans")
            shutil.rmtree(self.root / ".crew" / "reviews")

    def test_legacy_empty_task_and_unreadable_document_stay_bound(self) -> None:
        wait = self.legacy(task="")
        needs = self.confirm_legacy(wait)
        self.assertEqual(needs.question.kind, "legacy_requirements")
        decision = mt.MeasureDecision(needs.ref, needs.question.question_id, "requirements",
            requirements=mt.CapturedRequirements(needs.question.question_id, ("scope", "constraints", "checks")))
        self.assertEqual(mt.decide_measure_twice(needs.ref, decision).work_items[0].kind, "advisor")
        loop_state.resolve("mt", "isolated").unlink()
        wait = self.legacy(task="missing.md")
        self.assertEqual(self.confirm_legacy(wait).question.kind, "legacy_requirements")

    def test_legacy_done_only_finalizes_preserving_forced_history(self) -> None:
        terminal = self.legacy("done", verdict="APPROVED", content=b"# Old approved")
        self.assertEqual(terminal.outcome["status"], "approved")
        self.assertEqual(terminal.outcome["overrides"], ["quorum-not-met"])
        self.assertEqual(self.state()["stop_fires"], 7)
        self.assertIsNone(mt.journal_from_dict(self.state()["mt_workflow"]).action)

    def test_done_before_deactivate_recovery_uses_no_paid_action(self) -> None:
        with mock.patch.object(loop_state, "deactivate", side_effect=OSError("crash after done")), self.assertRaises(OSError):
            self.panel(self.draft())
        self.assertEqual(self.state()["phase"], "done")
        self.assertTrue(self.state()["active"])
        before = mt.journal_from_dict(self.state()["mt_workflow"])
        terminal = mt.next_measure_twice(mt._ref(self.state()))
        after = mt.journal_from_dict(self.state()["mt_workflow"])
        self.assertEqual(terminal.outcome["status"], "approved")
        self.assertEqual((before.action_ordinal, before.review_generation), (after.action_ordinal, after.review_generation))

    def test_safety_restart_requires_compatibility_force_and_is_adopted_once(self) -> None:
        step = self.start()
        loop_state.mutate(loop_state.resolve("mt", "isolated"), lambda data: {**data, "stop_fires": 150})
        terminal = mt.next_measure_twice(step.ref)
        refused = self.cli("state", "init", "mt", "--auto-plan", "--task", "--seats opus requirements.md", "--session-id", "isolated")
        self.assertNotEqual(refused.returncode, 0)
        restarted = self.cli("state", "init", "mt", "--auto-plan", "--task", "--seats opus requirements.md", "--session-id", "isolated", "--force")
        self.assertEqual(restarted.returncode, 0, restarted.stdout + restarted.stderr)
        wait = self.start()
        self.assertNotEqual(wait.ref, terminal.ref)
        self.assertEqual(self.state()["stop_fires"], 0)
        self.assertEqual(mt.start_measure_twice(mt.MeasureRequest("--seats opus requirements.md", "isolated")).ref, wait.ref)
        before = loop_state.resolve("mt", "isolated").read_bytes()
        active_restart = self.cli("state", "init", "mt", "--auto-plan", "--task", "task", "--session-id", "isolated", "--force")
        self.assertNotEqual(active_restart.returncode, 0)
        self.assertEqual(loop_state.resolve("mt", "isolated").read_bytes(), before)

    def test_old_advisor_late_write_cannot_change_replacement_canonical(self) -> None:
        old = self.start()
        item = old.work_items[0]
        mt.claim_measure_action(old.ref, item.action_id)
        mt.cancel_measure_twice(old.ref, "test replacement")
        restarted = self.cli("state", "init", "mt", "--auto-plan", "--task", self.request.raw_arguments, "--session-id", "isolated")
        self.assertEqual(restarted.returncode, 0, restarted.stdout)
        ready = self.confirm_legacy(self.start())
        panel = self.draft(ready)
        canonical = Path(self.state()["plan_file"])
        before = canonical.read_bytes()
        Path(item.staging_path).write_bytes(b"old action writes after replacement")
        self.assertEqual(canonical.read_bytes(), before)
        self.assertNotEqual(panel.ref, old.ref)
        with self.assertRaises(rw.WorkflowError):
            transport.capture_measure_return(old.ref, item.action_id, b"old completion")

    def test_drift_and_missing_target_park_without_certifying(self) -> None:
        for missing in (False, True):
            step = self.draft()
            canonical = Path(self.state()["plan_file"])
            if missing:
                canonical.unlink()
            else:
                canonical.write_bytes(b"drift")
            wait = self.panel(step)
            self.assertEqual(wait.question.kind, "completion_advisory")
            self.assertEqual(self.state()["last_verdict"], "")
            self.assertIn("target-no-longer-resolves" if missing else "target-drift", wait.question.text)
            mt.cancel_measure_twice(wait.ref, "next fixture")
            loop_state.resolve("mt", "isolated").unlink()

    def test_rising_findings_warning_is_bound_and_replay_safe(self) -> None:
        first = self.panel(self.draft(), "REVISE", blocking=1)
        second = self.panel(self.draft(first), "REVISE", blocking=2)
        self.assertEqual(second.question.kind, "rising_findings")
        self.assertEqual(self.state()["revision_round"], 2)
        self.assertEqual(mt.next_measure_twice(second.ref).question, second.question)
        with self.assertRaises(rw.WorkflowError):
            mt.decide_measure_twice(second.ref, mt.MeasureDecision(second.ref, "stale", "cancel"))
        ready = mt.decide_measure_twice(second.ref, mt.MeasureDecision(second.ref, second.question.question_id, "continue"))
        self.assertEqual(ready.work_items[0].kind, "advisor")
        self.assertEqual(self.state()["revision_round"], 2)

    def test_omitted_blocking_count_retains_prior_observation_and_explicit_zero_resets(self) -> None:
        first = self.panel(self.draft(), "REVISE", blocking=5)
        omitted = self.panel(self.draft(first), "REVISE", blocking=None)
        self.assertEqual(self.state()["mt_workflow"]["prior_blocking_count"], 5)
        self.assertEqual(omitted.type, "work_batch")
        third = self.panel(self.draft(omitted), "REVISE", blocking=8)
        self.assertEqual(third.question.kind, "rising_findings")
        self.assertIn("from 5 to 8", third.question.text)
        revision_round = self.state()["revision_round"]
        replay = self.cli("measure-twice-next", "--session-segment", third.ref.session_segment,
                          "--loop-instance-id", third.ref.loop_instance_id)
        self.assertEqual(replay.returncode, 0, replay.stdout)
        self.assertEqual(json.loads(replay.stdout)["question"]["question_id"], third.question.question_id)
        self.assertEqual(self.state()["revision_round"], revision_round)
        ready = mt.decide_measure_twice(third.ref, mt.MeasureDecision(third.ref, third.question.question_id, "continue"))
        zero = self.panel(self.draft(ready), "REVISE", blocking=0)
        self.assertEqual(self.state()["mt_workflow"]["prior_blocking_count"], 0)
        self.assertEqual(self.panel(self.draft(zero), "REVISE", blocking=8).type, "work_batch")

    def test_unreadable_feedback_commits_outcome_once_and_recovers_without_duplicate_work(self) -> None:
        for error in (OSError("feedback unavailable"), UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid feedback")):
            with self.subTest(error=error):
                panel = self.draft()
                for item in panel.work_items:
                    rw.claim_review_action(rw.ClaimRequest(item.review_ref, item.action_id))
                    transport.capture_review_return(item.review_ref, item.action_id, VALID)
                item = mt.next_measure_twice(panel.ref).work_items[0]
                rw.claim_review_action(rw.ClaimRequest(item.review_ref, item.action_id))
                transport.capture_review_return(item.review_ref, item.action_id, b"BLOCKING_CAUSES: 5\n",
                    judgment=transport.SynthesisJudgment("REVISE"))
                read_text = Path.read_text
                def fail_feedback(path: Path, *args: object, **kwargs: object) -> str:
                    if str(path) == item.returned_path:
                        raise error
                    return read_text(path, *args, **kwargs)
                with mock.patch.object(Path, "read_text", fail_feedback):
                    wait = mt.next_measure_twice(panel.ref)
                    self.assertEqual(wait.question.kind, "feedback_recovery")
                    self.assertEqual(mt.next_measure_twice(panel.ref).question, wait.question)
                    journal = mt.journal_from_dict(self.state()["mt_workflow"])
                    self.assertEqual(len(journal.applied_outcomes), 1)
                    self.assertEqual(journal.prior_blocking_count, 0)
                    self.assertIsNone(journal.action)
                    self.assertEqual(self.state()["revision_round"], 1)
                recovered = self.cli("measure-twice-resume", "--session-id", "isolated")
                self.assertEqual(recovered.returncode, 0, recovered.stdout)
                self.assertEqual(json.loads(recovered.stdout)["work_items"][0]["kind"], "advisor")
                self.assertEqual(self.state()["mt_workflow"]["prior_blocking_count"], 5)
                self.assertEqual(self.state()["revision_round"], 1)
                self.assertEqual(len(self.state()["mt_workflow"]["applied_outcomes"]), 1)
                mt.cancel_measure_twice(panel.ref, "next isolated failure")
                shutil.rmtree(self.root / ".crew")

    def test_concurrent_stop_counter_and_submission_both_survive(self) -> None:
        step = self.start()
        item = step.work_items[0]
        mt.claim_measure_action(step.ref, item.action_id)
        Path(item.staging_path).write_bytes(b"# Concurrent plan")
        barrier = threading.Barrier(2)
        def counter() -> None:
            barrier.wait(timeout=5)
            result = subprocess.run([sys.executable, str(Path(mt.__file__).parents[1] / "persistent-mode.py")],
                input=json.dumps({"session_id": "isolated", "cwd": str(self.root)}), env=dict(os.environ),
                capture_output=True, text=True, timeout=7)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["decision"], "block")
        def complete() -> None:
            barrier.wait(timeout=5)
            transport.capture_measure_return(step.ref, item.action_id, b"completed")
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(counter), pool.submit(complete)]
            for future in futures:
                future.result(timeout=10)
        self.assertEqual(self.state()["stop_fires"], 1)
        self.assertEqual(mt.journal_from_dict(self.state()["mt_workflow"]).stage, "reviewing")

    def test_state_lock_timeout_is_typed_and_never_partial(self) -> None:
        step = self.start()
        path = loop_state.resolve("mt", "isolated")
        before = path.read_bytes()
        with models.state_lock(path), self.assertRaises(loop_state.LoopStateError) as caught:
            mt.claim_measure_action(step.ref, step.work_items[0].action_id)
        self.assertEqual(caught.exception.code, "state_lock_timeout")
        self.assertEqual(path.read_bytes(), before)

    def test_bound_and_pending_review_refs_protect_runs_without_pointers(self) -> None:
        step = self.draft()
        ref = step.work_items[0].review_ref
        keys = artifact_prune.live_run_keys(self.root / ".crew")
        self.assertIn((ref.session_segment, ref.run_id), keys)
        run = self.root / ".crew" / "reviews" / ref.session_segment / ref.run_id
        self.assertFalse(rw._standalone_run_terminal_for_prune(run, ref.session_segment))
        mt.cancel_measure_twice(step.ref, "terminal artifacts available for cleanup")
        self.assertTrue(rw._standalone_run_terminal_for_prune(run, ref.session_segment))
        self.assertTrue(run.is_dir())

    def test_pinned_schema3_reader_mutation_and_cleanup_refuse_schema4_both_loops(self) -> None:
        fixture_dir = Path(__file__).parent / "fixtures"
        sys.path.insert(0, str(fixture_dir))
        self.addCleanup(sys.path.remove, str(fixture_dir))
        old = importlib.import_module("schema3_models")
        cleanup = importlib.import_module("schema3_cleanup")
        self.assertEqual(old.SCHEMA_VERSION, 3)
        baseline = json.loads((fixture_dir / "measure_twice_baseline_trace.json").read_text())
        pinned_bytes = (fixture_dir / "schema3_models.py").read_bytes().split(b"\n", 1)[1]
        self.assertEqual(mt.sha256(pinned_bytes), baseline["schema3_models_source_sha256"])
        for prefix in ("measure-twice-state", "build-state"):
            path = self.root / ".crew" / f"{prefix}-isolated.json"
            models.atomic_write_json(path, {"schema": 4, "active": True, "mt_workflow": {"future-data": "keep"}})
            before = path.read_bytes()
            self.assertEqual(old.LoopState.load_with_status(path)[1], old.LOAD_FUTURE_SCHEMA)
            invoked = []
            self.assertEqual(old.update_state_json(path, lambda data: invoked.append(data))[1], old.LOAD_FUTURE_SCHEMA)
            self.assertEqual(invoked, [])
            os.utime(path, (time.time() - 10 * 86400,) * 2)
            cleanup.cleanup_stale_files(path.parent)
            self.assertEqual(path.read_bytes(), before)

    def test_malformed_journal_refuses_without_partial_state(self) -> None:
        step = self.start()
        path = loop_state.resolve("mt", "isolated")
        for key, value in (("applied_outcomes", None), ("request_id", 7), ("requirements", []), ("question", {"wrong": 1})):
            original = self.state()
            changed = {**original, "mt_workflow": {**original["mt_workflow"], key: value}}
            models.atomic_write_json(path, changed)
            before = path.read_bytes()
            with self.subTest(key=key), self.assertRaises(rw.WorkflowError):
                mt.next_measure_twice(step.ref)
            self.assertEqual(path.read_bytes(), before)
            models.atomic_write_json(path, original)


    def test_stop_hook_fails_open_during_owner_reconciliation_lock(self) -> None:
        step = self.start()
        path = loop_state.resolve("mt", "isolated")
        before = path.read_bytes()
        hook = Path(mt.__file__).parents[1] / "persistent-mode.py"
        with models.state_lock(path):
            result = subprocess.run([sys.executable, str(hook)], input=json.dumps({"session_id": "isolated", "cwd": str(self.root)}),
                capture_output=True, text=True, timeout=7, env=dict(os.environ))
        self.assertEqual(result.returncode, 0)
        self.assertNotEqual(json.loads(result.stdout).get("decision"), "block")
        self.assertIn("lock", result.stdout + result.stderr)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(mt.next_measure_twice(step.ref).type, "work_batch")

    def test_safety_bound_between_review_claim_and_acceptance_refuses(self) -> None:
        step = self.draft()
        item = step.work_items[0]
        rw.claim_review_action(rw.ClaimRequest(item.review_ref, item.action_id))
        path = loop_state.resolve("mt", "isolated")
        loop_state.mutate(path, lambda data: {**data, "stop_fires": 150})
        before = path.read_bytes()
        with self.assertRaises(rw.WorkflowError):
            transport.capture_review_return(item.review_ref, item.action_id, VALID)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(mt.next_measure_twice(step.ref).outcome["status"], "force_exit")

    def test_synthesis_retry_receipt_reconciles_after_loop_write_crash(self) -> None:
        wait = self.panel(self.draft(), synthesis_fail=True)
        original = mt._transaction
        calls = 0
        def transaction(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("crash after review retry receipt")
            return original(*args, **kwargs)
        decision = mt.MeasureDecision(wait.ref, wait.question.question_id, "retry_synthesis")
        with mock.patch.object(mt, "_transaction", side_effect=transaction), self.assertRaises(OSError):
            mt.decide_measure_twice(wait.ref, decision)
        again = mt.decide_measure_twice(wait.ref, decision)
        self.assertEqual([item.kind for item in again.work_items], ["synthesis"])
        self.assertEqual(again.work_items[0].review_ref.attempt_id, "attempt-0002")
        self.assertEqual(self.state()["consecutive_review_failures"], 0)

    def review_flags(self, ref: rw.ReviewRef) -> tuple[str, ...]:
        return ("--session-segment", ref.session_segment, "--run-id", ref.run_id,
                "--attempt-id", ref.attempt_id, "--target-sha256", ref.target_sha256)

    def test_loop_public_retry_requires_durable_matching_decision(self) -> None:
        for synthesis in (True, False):
            with self.subTest(synthesis=synthesis):
                wait = self.panel(self.draft(), synthesis_fail=synthesis, failed=0 if synthesis else 1)
                ref = mt.journal_from_dict(self.state()["mt_workflow"]).review_ref
                before = self.filesystem_snapshot()
                for selection in ((), ("--synthesis-only",)):
                    result = self.cli("review-retry", *self.review_flags(ref), *selection)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(json.loads(result.stdout)["code"], "retry_not_authorized")
                    self.assertEqual(self.filesystem_snapshot(), before)
                self.assertEqual(rw.next_loop_review(ref).type, "terminal")
                if not synthesis:
                    self.assertEqual(rw.read_loop_review_evidence(ref).source.ref, ref)
                mt.cancel_measure_twice(wait.ref, "cleanup")
                loop_state.resolve("mt", "isolated").unlink()

    def test_owner_human_wait_blocks_new_claims_and_external_execution_but_allows_settlement(self) -> None:
        self.request = dataclasses.replace(self.request, raw_arguments="--seats sol,opus requirements.md")
        panel = self.draft()
        native_item = next(item for item in panel.work_items if item.driver == "native")
        external = next(item for item in panel.work_items if item.driver == "external")
        rw.claim_review_action(rw.ClaimRequest(native_item.review_ref, native_item.action_id))
        question = mt._bound_question(panel.ref, "rising_findings", "A bound human pause")
        loop_state.mutate(loop_state.resolve("mt", "isolated"), lambda data: {
            **data, "awaiting_input": True, "mt_workflow": {**data["mt_workflow"], "question": dataclasses.asdict(question)}})
        before = self.filesystem_snapshot()
        with mock.patch.object(rw, "_frozen_external_provider") as provider, self.assertRaises(rw.WorkflowError) as refused:
            rw.execute_external_review(external.review_ref, external.action_id)
        self.assertEqual(refused.exception.code, "work_not_admitted")
        provider.assert_not_called()
        self.assertEqual(self.filesystem_snapshot(), before)
        result = self.cli("review-execute", *self.review_flags(external.review_ref), "--action-id", external.action_id)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(json.loads(result.stdout)["code"], "work_not_admitted")
        self.assertEqual(self.filesystem_snapshot(), before)
        transport.capture_review_return(native_item.review_ref, native_item.action_id, VALID)
        self.assertEqual(mt.next_measure_twice(panel.ref).question.question_id, question.question_id)
        # A ready parent action must not receive perform during the same pause.
        mt.cancel_measure_twice(panel.ref, "cleanup")
        loop_state.resolve("mt", "isolated").unlink()
        self.request = dataclasses.replace(self.request, raw_arguments="--seats opus,sonnet requirements.md")
        panel = self.draft()
        for item in panel.work_items:
            rw.claim_review_action(rw.ClaimRequest(item.review_ref, item.action_id))
            transport.capture_review_return(item.review_ref, item.action_id, VALID)
        synthesis = mt.next_measure_twice(panel.ref).work_items[0]
        loop_state.mutate(loop_state.resolve("mt", "isolated"), lambda data: {
            **data, "awaiting_input": True, "mt_workflow": {**data["mt_workflow"], "question": dataclasses.asdict(question)}})
        before = self.filesystem_snapshot()
        result = self.cli("review-claim", *self.review_flags(synthesis.review_ref), "--action-id", synthesis.action_id)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(json.loads(result.stdout)["code"], "work_not_admitted")
        self.assertEqual(self.filesystem_snapshot(), before)

    def test_authorized_retry_crashes_resume_once_through_public_next_and_resume(self) -> None:
        for synthesis in (True, False):
            for committed in (False, True):
                with self.subTest(synthesis=synthesis, committed=committed):
                    wait = self.panel(self.draft(), synthesis_fail=synthesis, failed=0 if synthesis else 1)
                    before = mt.journal_from_dict(self.state()["mt_workflow"])
                    decision = mt.MeasureDecision(wait.ref, wait.question.question_id,
                                                  "retry_synthesis" if synthesis else "retry_review")
                    retry = rw._retry_loop_review_under_owner_lock
                    def crash(request: rw.RetryRequest) -> rw.ReviewStep:
                        if committed:
                            retry(request)
                        raise OSError("retry commit fault")
                    with mock.patch.object(rw, "_retry_loop_review_under_owner_lock", side_effect=crash), self.assertRaises(OSError):
                        mt.decide_measure_twice(wait.ref, decision)
                    saved = mt.journal_from_dict(self.state()["mt_workflow"])
                    self.assertEqual(saved.retry_authorization.decision, decision)
                    self.assertEqual(saved.retry_authorization.source_ref, before.review_ref)
                    self.assertEqual(saved.review_ref, before.review_ref)
                    self.assertEqual(saved.question, wait.question)
                    run = self.root / ".crew" / "reviews" / "isolated" / before.review_ref.run_id
                    wf = rw._workflow(run)
                    self.assertEqual(len(wf["retry_receipts"]), int(committed))
                    if committed:
                        current = rw.parse_review_ref(wf["ref"])
                        action = next(action for action in wf["actions"] if action["attempt_id"] == current.attempt_id)
                        snapshot = self.filesystem_snapshot()
                        result = self.cli("review-claim", *self.review_flags(current), "--action-id", action["action_id"])
                        self.assertEqual(result.returncode, 2, result.stderr)
                        self.assertEqual(json.loads(result.stdout)["code"], "work_not_admitted")
                        self.assertEqual(self.filesystem_snapshot(), snapshot)
                    args = (("measure-twice-resume", "--session-id", "isolated") if committed else
                            ("measure-twice-next", "--session-segment", "isolated", "--loop-instance-id", wait.ref.loop_instance_id))
                    result = self.cli(*args)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    resumed = json.loads(result.stdout)
                    self.assertEqual(resumed["type"], "work_batch")
                    self.assertEqual([item["kind"] for item in resumed["work_items"]], ["synthesis" if synthesis else "reviewer"])
                    self.assertEqual(resumed["work_items"][0]["review_ref"]["attempt_id"], "attempt-0002")
                    after = mt.journal_from_dict(self.state()["mt_workflow"])
                    self.assertIsNone(after.question)
                    self.assertFalse(self.state()["awaiting_input"])
                    self.assertEqual(after.review_ref.attempt_id, "attempt-0002")
                    self.assertEqual(after.accepted_actions, before.accepted_actions)
                    wf = rw._workflow(run)
                    actions = wf["actions"]
                    self.assertEqual(len(wf["retry_receipts"]), 1)
                    self.assertEqual(mt.step_to_dict(mt.next_measure_twice(wait.ref)), resumed)
                    self.assertEqual(rw._workflow(run)["actions"], actions)
                    snapshot = self.filesystem_snapshot()
                    with self.assertRaises(rw.WorkflowError) as stale:
                        mt.decide_measure_twice(wait.ref, decision)
                    self.assertEqual(stale.exception.code, "stale_question")
                    self.assertEqual(self.filesystem_snapshot(), snapshot)
                    item = mt.next_measure_twice(wait.ref).work_items[0]
                    rw.claim_review_action(rw.ClaimRequest(item.review_ref, item.action_id))
                    mt.cancel_measure_twice(wait.ref, "cleanup")
                    loop_state.resolve("mt", "isolated").unlink()

    def test_human_pause_during_external_probe_blocks_claim_and_provider_run(self) -> None:
        self.request = dataclasses.replace(self.request, raw_arguments="--seats sol requirements.md")
        panel = self.draft()
        item = panel.work_items[0]
        provider = mock.Mock()
        def pause() -> tuple[bool, str]:
            question = mt._bound_question(panel.ref, "rising_findings", "Pause during provider probing")
            loop_state.mutate(loop_state.resolve("mt", "isolated"), lambda data: {
                **data, "awaiting_input": True, "mt_workflow": {**data["mt_workflow"], "question": dataclasses.asdict(question)}})
            return True, "available"
        provider.is_available.side_effect = pause
        with mock.patch.object(rw, "_frozen_external_provider", return_value=provider), self.assertRaises(rw.WorkflowError) as refused:
            rw.execute_external_review(item.review_ref, item.action_id)
        self.assertEqual(refused.exception.code, "work_not_admitted")
        provider.run.assert_not_called()
        run = self.root / ".crew" / "reviews" / "isolated" / item.review_ref.run_id
        self.assertEqual(rw._workflow(run)["actions"][0]["status"], "ready")

    def test_pending_decision_replay_reconciles_committed_retry_without_new_actions(self) -> None:
        wait = self.panel(self.draft(), synthesis_fail=True)
        decision = mt.MeasureDecision(wait.ref, wait.question.question_id, "retry_synthesis")
        retry = rw._retry_loop_review_under_owner_lock
        def crash(request: rw.RetryRequest) -> rw.ReviewStep:
            retry(request)
            raise OSError("after durable retry receipt")
        with mock.patch.object(rw, "_retry_loop_review_under_owner_lock", side_effect=crash), self.assertRaises(OSError):
            mt.decide_measure_twice(wait.ref, decision)
        source = mt.journal_from_dict(self.state()["mt_workflow"]).review_ref
        run = self.root / ".crew" / "reviews" / "isolated" / source.run_id
        actions = rw._workflow(run)["actions"]
        resumed = mt.decide_measure_twice(wait.ref, decision)
        self.assertEqual(resumed.work_items[0].review_ref.attempt_id, "attempt-0002")
        self.assertEqual(rw._workflow(run)["actions"], actions)
        self.assertEqual(len(rw._workflow(run)["retry_receipts"]), 1)
        self.assertFalse(self.state()["awaiting_input"])

    def test_pending_retry_rejects_wrong_question_attempt_owner_and_selection(self) -> None:
        wait = self.panel(self.draft(), synthesis_fail=True)
        decision = mt.MeasureDecision(wait.ref, wait.question.question_id, "retry_synthesis")
        with mock.patch.object(rw, "_retry_loop_review_under_owner_lock", side_effect=OSError("before commit")), self.assertRaises(OSError):
            mt.decide_measure_twice(wait.ref, decision)
        original = self.state()
        ref = mt.journal_from_dict(original["mt_workflow"]).review_ref
        for field, value in (("question_id", mt.sha256(b"wrong question")), ("attempt_id", "attempt-0007"),
                             ("loop_instance_id", "wrong-owner"), ("kind", "retry_review")):
            changed = json.loads(json.dumps(original))
            authorization = changed["mt_workflow"]["retry_authorization"]
            if field == "attempt_id":
                authorization["source_ref"][field] = value
            elif field == "loop_instance_id":
                authorization["decision"]["ref"][field] = value
            else:
                authorization["decision"][field] = value
            models.atomic_write_json(loop_state.resolve("mt", "isolated"), changed)
            before = self.filesystem_snapshot()
            result = self.cli("review-retry", *self.review_flags(ref), "--synthesis-only")
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(json.loads(result.stdout)["code"], "retry_not_authorized")
            self.assertEqual(self.filesystem_snapshot(), before)
        models.atomic_write_json(loop_state.resolve("mt", "isolated"), original)
        before = self.filesystem_snapshot()
        result = self.cli("review-retry", *self.review_flags(ref))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(json.loads(result.stdout)["code"], "retry_not_authorized")
        self.assertEqual(self.filesystem_snapshot(), before)

    def test_pending_retry_cancel_replacement_and_bounds_win_before_commit(self) -> None:
        for committed in (False, True):
            for ending in ("cancel", "replacement", "bound"):
                with self.subTest(ending=ending, committed=committed):
                    wait = self.panel(self.draft(), synthesis_fail=True)
                    decision = mt.MeasureDecision(wait.ref, wait.question.question_id, "retry_synthesis")
                    retry = rw._retry_loop_review_under_owner_lock
                    def crash(request: rw.RetryRequest) -> rw.ReviewStep:
                        if committed:
                            retry(request)
                        raise OSError("retry commit fault")
                    with mock.patch.object(rw, "_retry_loop_review_under_owner_lock", side_effect=crash), self.assertRaises(OSError):
                        mt.decide_measure_twice(wait.ref, decision)
                    ref = mt.journal_from_dict(self.state()["mt_workflow"]).review_ref
                    run = self.root / ".crew" / "reviews" / "isolated" / ref.run_id
                    if ending == "bound":
                        loop_state.mutate(loop_state.resolve("mt", "isolated"), lambda data: {**data, "stop_fires": 150})
                        self.assertEqual(mt.next_measure_twice(wait.ref).outcome["status"], "force_exit")
                    else:
                        mt.cancel_measure_twice(wait.ref, "before authorized retry")
                        if ending == "replacement":
                            replacement = mt.start_measure_twice(dataclasses.replace(self.request, raw_arguments="--seats opus requirements.md"))
                            snapshot = self.filesystem_snapshot()
                            with self.assertRaises(rw.WorkflowError) as stale:
                                mt.next_measure_twice(wait.ref)
                            self.assertEqual(stale.exception.code, "stale_owner")
                            self.assertEqual(self.filesystem_snapshot(), snapshot)
                            mt.cancel_measure_twice(replacement.ref, "cleanup")
                        else:
                            self.assertEqual(mt.next_measure_twice(wait.ref).outcome["status"], "cancelled")
                    self.assertEqual(len(rw._workflow(run)["retry_receipts"]), int(committed))
                    current = rw.parse_review_ref(rw._workflow(run)["ref"])
                    snapshot = self.filesystem_snapshot()
                    with self.assertRaises(rw.WorkflowError):
                        rw.retry_review(rw.RetryRequest(current, ()))
                    self.assertEqual(self.filesystem_snapshot(), snapshot)
                    loop_state.resolve("mt", "isolated").unlink()

    def test_advisor_capture_retains_bytes_before_submit_crash_and_public_replay(self) -> None:
        for remove in (False, True):
            with self.subTest(remove=remove):
                step = self.start()
                item = step.work_items[0]
                mt.claim_measure_action(step.ref, item.action_id)
                observed = "# Exact plan\r\nBytes: π\r\n".encode()
                Path(item.staging_path).write_bytes(observed)
                raw = b"exact hand-back\r\nwithout final newline"
                with mock.patch.object(mt, "submit_measure_action", side_effect=OSError("capture/submit fault")), self.assertRaises(OSError):
                    transport.capture_measure_return(step.ref, item.action_id, raw)
                journal = mt.journal_from_dict(self.state()["mt_workflow"])
                root = mt._namespace(step.ref, journal)
                receipt = json.loads((root / "transport-receipts" / f"{item.action_id}.json").read_bytes())
                retained = mt.parse_measure_result(receipt["result"])
                self.assertEqual(retained.status, "ok")
                self.assertEqual(mt._sealed_plan_path(retained, journal).read_bytes(), observed)
                if remove:
                    Path(item.staging_path).unlink()
                else:
                    Path(item.staging_path).write_bytes(b"# Late staging overwrite")
                ingress = root / "host-return" / f"{item.action_id}.txt"
                result = self.cli("measure-twice-capture", "--session-segment", "isolated",
                    "--loop-instance-id", step.ref.loop_instance_id, "--action-id", item.action_id, "-f", str(ingress))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["work_items"][0]["kind"], "reviewer")
                self.assertEqual(Path(self.state()["plan_file"]).read_bytes(), observed)
                self.assertEqual(ingress.read_bytes(), raw)
                self.assertEqual(len(self.state()["mt_workflow"]["accepted_actions"]), 1)
                transport.capture_measure_return(step.ref, item.action_id, raw)
                self.assertEqual(len(self.state()["mt_workflow"]["accepted_actions"]), 1)
                mt.cancel_measure_twice(step.ref, "cleanup")
                loop_state.resolve("mt", "isolated").unlink()

    def test_advisor_capture_receipt_survives_owner_write_crash_without_mutable_staging(self) -> None:
        step = self.start()
        item = step.work_items[0]
        mt.claim_measure_action(step.ref, item.action_id)
        Path(item.staging_path).write_bytes(b"# Observation before journal write")
        path = loop_state.resolve("mt", "isolated")
        before = path.read_bytes()
        atomic = models.atomic_write_json
        def crash(target: Path, data: object) -> None:
            if target == path:
                raise OSError("capture owner commit fault")
            atomic(target, data)
        with mock.patch.object(models, "atomic_write_json", side_effect=crash), self.assertRaises(OSError):
            transport.capture_measure_return(step.ref, item.action_id, b"completed observation")
        self.assertEqual(path.read_bytes(), before)
        Path(item.staging_path).unlink()
        panel = transport.capture_measure_return(step.ref, item.action_id, b"completed observation")
        self.assertEqual(panel.work_items[0].kind, "reviewer")
        self.assertEqual(Path(self.state()["plan_file"]).read_bytes(), b"# Observation before journal write")

    def test_capture_conflicts_do_not_overwrite_ingress_or_replace_retained_plan(self) -> None:
        step = self.start()
        item = step.work_items[0]
        mt.claim_measure_action(step.ref, item.action_id)
        Path(item.staging_path).write_bytes(b"# Observed candidate")
        with mock.patch.object(mt, "submit_measure_action", side_effect=OSError("before submit")), self.assertRaises(OSError):
            transport.capture_measure_return(step.ref, item.action_id, b"observed return")
        before = self.filesystem_snapshot()
        with self.assertRaises(rw.WorkflowError) as conflict:
            transport.capture_measure_return(step.ref, item.action_id, b"changed return")
        self.assertEqual(conflict.exception.code, "conflict")
        self.assertEqual(self.filesystem_snapshot(), before)
        Path(item.staging_path).write_bytes(b"# Replacement staging")
        different = mt.MeasureResult(step.ref, item.action_id, "ok", mt.sha256(b"# Replacement staging"), mt.sha256(b"observed return"))
        before = self.filesystem_snapshot()
        with self.assertRaises(rw.WorkflowError) as conflict:
            mt.submit_measure_action(mt.MeasureSubmission(different))
        self.assertEqual(conflict.exception.code, "conflict")
        self.assertEqual(self.filesystem_snapshot(), before)
        transport.capture_measure_return(step.ref, item.action_id, b"observed return")
        self.assertEqual(Path(self.state()["plan_file"]).read_bytes(), b"# Observed candidate")

    def test_direct_submit_without_transport_receipt_and_conflicting_capture_leave_ingress_intact(self) -> None:
        step = self.start()
        item = step.work_items[0]
        mt.claim_measure_action(step.ref, item.action_id)
        observed = b"# Direct submitted plan"
        Path(item.staging_path).write_bytes(observed)
        result = mt.MeasureResult(step.ref, item.action_id, "ok", mt.sha256(observed), mt.sha256(b"direct return"))
        with mock.patch.object(mt, "next_measure_twice", return_value=mt.MeasureStep("waiting", step.ref)):
            mt.submit_measure_action(mt.MeasureSubmission(result))
        journal = mt.journal_from_dict(self.state()["mt_workflow"])
        root = mt._namespace(step.ref, journal)
        self.assertFalse((root / "transport-receipts" / f"{item.action_id}.json").exists())
        ingress = root / "host-return" / f"{item.action_id}.txt"
        ingress.parent.mkdir(parents=True, exist_ok=True)
        ingress.write_bytes(b"direct return")
        before = self.filesystem_snapshot()
        with self.assertRaises(rw.WorkflowError) as conflict:
            transport.capture_measure_return(step.ref, item.action_id, b"different return")
        self.assertEqual(conflict.exception.code, "conflict")
        self.assertEqual(self.filesystem_snapshot(), before)
        Path(item.staging_path).unlink()
        self.assertEqual(mt.submit_measure_action(mt.MeasureSubmission(result)).work_items[0].kind, "reviewer")
        self.assertEqual(Path(self.state()["plan_file"]).read_bytes(), observed)

    def test_capture_sealed_bytes_guard_and_cancellation_prevent_promotion(self) -> None:
        for cancel in (False, True):
            step = self.start()
            item = step.work_items[0]
            mt.claim_measure_action(step.ref, item.action_id)
            Path(item.staging_path).write_bytes(b"# Retained observation")
            with mock.patch.object(mt, "submit_measure_action", side_effect=OSError("before submit")), self.assertRaises(OSError):
                transport.capture_measure_return(step.ref, item.action_id, b"done")
            journal = mt.journal_from_dict(self.state()["mt_workflow"])
            if cancel:
                mt.cancel_measure_twice(step.ref, "before promotion")
            else:
                root = mt._namespace(step.ref, journal)
                result = mt.parse_measure_result(json.loads((root / "transport-receipts" / f"{item.action_id}.json").read_bytes())["result"])
                mt._sealed_plan_path(result, journal).write_bytes(b"# Corrupt sealed bytes")
            before = self.filesystem_snapshot()
            with self.assertRaises(rw.WorkflowError):
                transport.capture_measure_return(step.ref, item.action_id, b"done")
            self.assertEqual(self.filesystem_snapshot(), before)
            self.assertEqual(self.state()["plan_file"], "")
            if not cancel:
                mt.cancel_measure_twice(step.ref, "cleanup")
            loop_state.resolve("mt", "isolated").unlink()

    def test_formatter_failure_retains_raw_evidence_without_failed_verdict(self) -> None:
        step = self.draft()
        raw = b"An unstructured but useful review. Inspect the source."
        for item in step.work_items:
            rw.claim_review_action(rw.ClaimRequest(item.review_ref, item.action_id))
            transport.capture_review_return(item.review_ref, item.action_id, raw)
        repair = mt.next_measure_twice(step.ref)
        self.assertEqual({item.kind for item in repair.work_items}, {"formatter"})
        for item in repair.work_items:
            rw.claim_review_action(rw.ClaimRequest(item.review_ref, item.action_id))
            transport.capture_review_return(item.review_ref, item.action_id, b"", status="failed", diagnostic="formatter unavailable")
        synthesis = mt.next_measure_twice(step.ref).work_items[0]
        prompt = Path(synthesis.prompt_path).read_text()
        full = self.root / ".crew" / "reviews" / "isolated" / synthesis.review_ref.run_id / "panel-full.md"
        self.assertIn("singleton BLOCKING", prompt)
        self.assertIn(raw.decode(), full.read_text())
        self.assertEqual(self.state()["consecutive_review_failures"], 0)
        self.assertEqual(self.state()["last_verdict"], "")

    def test_promotion_crash_after_seal_before_checkpoint_replays_same_bytes(self) -> None:
        step = self.start()
        item = step.work_items[0]
        mt.claim_measure_action(step.ref, item.action_id)
        Path(item.staging_path).write_bytes(b"# Seal candidate")
        result = mt.MeasureResult(step.ref, item.action_id, "ok", mt.sha256(b"# Seal candidate"), mt.sha256(b"done"))
        write = mt._write_once
        def crash(path: Path, content: bytes, root: Path) -> None:
            write(path, content, root)
            if "sealed" in path.parts:
                raise OSError("crash after seal")
        with mock.patch.object(mt, "_write_once", side_effect=crash), self.assertRaises(OSError):
            mt.submit_measure_action(mt.MeasureSubmission(result))
        self.assertEqual(mt.journal_from_dict(self.state()["mt_workflow"]).action.status, "claimed")
        self.assertEqual(mt.submit_measure_action(mt.MeasureSubmission(result)).work_items[0].kind, "reviewer")
        changed = dataclasses.replace(result, plan_sha256=mt.sha256(b"changed"))
        with self.assertRaises(rw.WorkflowError):
            mt.submit_measure_action(mt.MeasureSubmission(changed))

    def test_advisor_and_reviewer_capture_reject_unissued_paths(self) -> None:
        step = self.start()
        item = step.work_items[0]
        mt.claim_measure_action(step.ref, item.action_id)
        Path(item.staging_path).write_bytes(b"# Plan")
        with self.assertRaises(rw.WorkflowError):
            transport.capture_measure_return(step.ref, item.action_id, b"done", capture_path=str(self.root / "foreign.txt"))
        self.assertFalse((self.root / "foreign.txt").exists())
        panel = transport.capture_measure_return(step.ref, item.action_id, b"done")
        reviewer = panel.work_items[0]
        rw.claim_review_action(rw.ClaimRequest(reviewer.review_ref, reviewer.action_id))
        with self.assertRaises(rw.WorkflowError):
            transport.capture_review_return(reviewer.review_ref, reviewer.action_id, VALID, capture_path=str(self.root / "foreign.txt"))
        self.assertFalse((self.root / "foreign.txt").exists())

    def test_code_runtime_uses_issued_provider_labelled_pins_without_scribes(self) -> None:
        pins = mock.patch.object(seats, "TASK_MODEL_ALIASES",
            seats.TASK_MODEL_ALIASES | {"provider-a/model-one", "provider-b/model-two"})
        pins.start()
        self.addCleanup(pins.stop)
        directory = self.root / ".crew"
        directory.mkdir()
        (directory / "config.toml").write_text('[seats.voice_a]\nvia=["claude"]\nmodel="provider-a/model-one"\n'
                                               '[seats.voice_b]\nvia=["claude"]\nmodel="provider-b/model-two"\n')
        self.request = dataclasses.replace(self.request, raw_arguments="--seats voice_a,voice_b requirements.md")
        step = self.draft()
        launches: list[mt.MeasureWorkItem] = []
        intervals: list[tuple[str, int, int]] = []
        class Runtime:
            def launch(self, item: mt.MeasureWorkItem) -> str:
                if item.role == "crew:scribe":
                    raise AssertionError("no scribe admission")
                launches.append(item)
                return item.action_id
            def completions(self, handles: tuple[str, ...]):
                if len(launches) != 2:
                    raise AssertionError("native work was serialized")
                for finished, handle in enumerate(handles, 1):
                    intervals.append((handle, 0, finished))
                    yield transport.Completion(handle, VALID)
        result = transport.run_measure_batch(step, Runtime())
        self.assertEqual([item.model for item in launches], ["provider-a/model-one", "provider-b/model-two"])
        self.assertEqual(result.work_items[0].kind, "synthesis")
        self.assertEqual(len(intervals), 2)
        run = self.root / ".crew" / "reviews" / "isolated" / step.work_items[0].review_ref.run_id
        self.assertEqual((run / "panel-full.md").read_text().count("## FINDINGS"), 2)
        # These are test pins on the existing Claude native route, not new production hosts/providers.

    def compatibility_panel(self, loop: str) -> tuple[Path, Path, bytes]:
        rr = rw.review_runs
        target = self.root / "requirements.md"
        digest = rr.sha256_text(target.read_text(encoding="utf-8"))
        signatures = {"sol": {"kind": "subprocess", "provider": "codex", "model": "m"},
                      "opus": {"kind": "task", "model": "opus"}}
        run_id, identity = rr.mint_identity(target_sha256=digest, target_spec=str(target),
            target_base="", seat_signatures=signatures)
        reviews = self.root / ".crew" / "reviews" / "isolated"
        run = reviews / run_id
        run.mkdir(parents=True, exist_ok=True)
        rr.write_run_json_once(run, {"run_id": run_id, "identity_digest": identity,
            "target_sha256": digest, "target_spec": str(target), "target_base": "",
            "seat_signatures": signatures, "subprocess_seats": ["sol"], "task_seats": ["opus"]})
        rr.write_pointer(reviews, run_id=run_id, target_sha256=digest, created_at=models.utc_now_iso())
        task_flag = "--prompt" if loop == "bl" else "--task"
        plan_flags = ("--plan-file", str(target)) if loop == "mt" else ()
        initialized = self.cli("state", "init", loop, "--session-id", "isolated", task_flag, "fixture review", *plan_flags)
        self.assertEqual(initialized.returncode, 0, initialized.stdout + initialized.stderr)
        begun = self.cli("state", "begin-review", loop, "--session-id", "isolated")
        self.assertEqual(begun.returncode, 0, begun.stdout + begun.stderr)
        for seat in signatures:
            (run / f"{seat}.json").write_text(json.dumps({"name": seat, "ok": True, "run_id": run_id,
                "target_sha256": digest, "output": VALID.decode(), "error": None, "elapsed": 1.0}))
            self.assertTrue(rr.seat_landed_valid(run, seat, run_id, digest))
        return loop_state.resolve(loop, "isolated"), run, (run / "run.json").read_bytes()

    def test_compatibility_missing_manifest_ignores_surviving_seats_and_preserves_failed_progression(self) -> None:
        for loop in ("bl", "mt"):
            with self.subTest(loop=loop):
                state, run, manifest = self.compatibility_panel(loop)
                (run / "run.json").unlink()
                evidence = loop_state.legacy_evidence(loop_state.read(state), "isolated")
                self.assertIsNone(evidence.source.manifest_identity_digest)
                self.assertEqual(evidence.usable_seats, ())
                loop_state.validate_evidence(evidence)
                for verdict, extra in (("APPROVED", ()), ("REVISE", ("--minor-only",)),
                                       ("REVISE", ()), ("REJECT", ())):
                    before = state.read_bytes()
                    result = self.cli("state", "record-verdict", loop, verdict, *extra, "--session-id", "isolated")
                    self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
                    self.assertIn("zero-usable", result.stderr)
                    if verdict == "APPROVED" or extra:
                        self.assertIn("quorum-not-met", result.stderr)
                    self.assertEqual(state.read_bytes(), before)
                failed = self.cli("state", "record-verdict", loop, "FAILED", "--session-id", "isolated")
                self.assertEqual(failed.returncode, 0, failed.stdout + failed.stderr)
                data = loop_state.read(state)
                self.assertTrue(data["active"])
                self.assertEqual((data["phase"], data["consecutive_review_failures"]), ("drafting", 1))
                (run / "run.json").write_bytes(manifest)
                begun = self.cli("state", "begin-review", loop, "--session-id", "isolated")
                self.assertEqual(begun.returncode, 0, begun.stdout + begun.stderr)
                (run / "run.json").unlink()
                failed = self.cli("state", "record-verdict", loop, "FAILED", "--session-id", "isolated")
                self.assertEqual(failed.returncode, 0, failed.stdout + failed.stderr)
                data = loop_state.read(state)
                self.assertFalse(data["active"])
                self.assertEqual((data["exit_kind"], data["consecutive_review_failures"]), ("review_failed", 2))
                shutil.rmtree(self.root / ".crew")

    def test_compatibility_unreadable_or_corrupt_manifest_degrades_without_certifying_surviving_seats(self) -> None:
        for loop in ("bl", "mt"):
            for corruption in (b"{truncated", b"\xff", b"[]", "directory", "unreadable"):
                with self.subTest(loop=loop, corruption=corruption):
                    state, run, _manifest = self.compatibility_panel(loop)
                    path = run / "run.json"
                    if corruption == "directory":
                        path.unlink()
                        path.mkdir()
                    elif corruption == "unreadable":
                        path.chmod(0)
                        if os.access(path, os.R_OK):
                            path.chmod(0o600)
                            shutil.rmtree(self.root / ".crew")
                            continue  # A privileged test runner cannot simulate denied reads with mode bits.
                    else:
                        path.write_bytes(corruption)
                    try:
                        evidence = loop_state.legacy_evidence(loop_state.read(state), "isolated")
                        self.assertEqual(evidence.usable_seats, ())
                        self.assertIsNone(evidence.source.manifest_identity_digest)
                        before = state.read_bytes()
                        result = self.cli("state", "record-verdict", loop, "APPROVED", "--session-id", "isolated")
                        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
                        self.assertEqual(state.read_bytes(), before)
                        result = self.cli("state", "record-verdict", loop, "FAILED", "--session-id", "isolated")
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        self.assertEqual(loop_state.read(state)["consecutive_review_failures"], 1)
                    finally:
                        if corruption == "unreadable":
                            path.chmod(0o600)
                        shutil.rmtree(self.root / ".crew")

    def test_compatibility_readable_manifest_identity_tampering_remains_unforceable(self) -> None:
        for loop in ("bl", "mt"):
            for tamper in ("run_id", "identity_digest", "target_sha256", "missing_identity"):
                with self.subTest(loop=loop, tamper=tamper):
                    state, run, manifest = self.compatibility_panel(loop)
                    record = json.loads(manifest)
                    if tamper == "missing_identity":
                        record.pop("identity_digest")
                    else:
                        record[tamper] = "run-000000000000" if tamper == "run_id" else "0" * 64
                    (run / "run.json").write_text(json.dumps(record))
                    before = state.read_bytes()
                    for verdict in ("APPROVED", "FAILED"):
                        result = self.cli("state", "record-verdict", loop, verdict, "--force", "--session-id", "isolated")
                        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                        self.assertIn("identity check", result.stderr)
                        self.assertEqual(state.read_bytes(), before)
                    shutil.rmtree(self.root / ".crew")

    def test_compatibility_verified_manifest_retains_usable_evidence_and_normal_completion(self) -> None:
        for loop in ("bl", "mt"):
            with self.subTest(loop=loop):
                state, run, manifest = self.compatibility_panel(loop)
                evidence = loop_state.legacy_evidence(loop_state.read(state), "isolated")
                self.assertEqual(evidence.usable_seats, ("sol", "opus"))
                self.assertEqual(evidence.source.manifest_identity_digest, json.loads(manifest)["identity_digest"])
                before = state.read_bytes()
                refused = self.cli("state", "record-verdict", loop, "FAILED", "--session-id", "isolated")
                self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
                self.assertEqual(state.read_bytes(), before)
                approved = self.cli("state", "record-verdict", loop, "APPROVED", "--session-id", "isolated")
                self.assertEqual(approved.returncode, 0, approved.stdout + approved.stderr)
                self.assertEqual(loop_state.read(state)["phase"], "done")
                shutil.rmtree(self.root / ".crew")

    def test_pure_evidence_guard_rejects_mismatched_source_and_duplicate_roster(self) -> None:
        step = self.draft()
        for item in step.work_items:
            rw.claim_review_action(rw.ClaimRequest(item.review_ref, item.action_id))
            transport.capture_review_return(item.review_ref, item.action_id, VALID)
        synthesis = mt.next_measure_twice(step.ref).work_items[0]
        rw.claim_review_action(rw.ClaimRequest(synthesis.review_ref, synthesis.action_id))
        transport.capture_review_return(synthesis.review_ref, synthesis.action_id, b"approved", judgment=transport.SynthesisJudgment("APPROVED"))
        evidence = rw.read_loop_review_evidence(synthesis.review_ref)
        self.assertEqual(evidence.usable_seats, ("opus", "sonnet"))
        for invalid in (dataclasses.replace(evidence, usable_seats=("opus", "opus")),
                        dataclasses.replace(evidence, target_sha256="0" * 64),
                        dataclasses.replace(evidence, source=dataclasses.replace(evidence.source, accepted_outcome_sha256="short"))):
            with self.subTest(invalid=invalid), self.assertRaises(loop_state.LoopStateError):
                loop_state.apply_verdict(dict(self.state()), invalid, "APPROVED")
        with self.assertRaises(loop_state.LoopStateError):
            loop_state.apply_verdict(dict(self.state()), evidence, "FAILED")


    def test_panel_selection_preserves_explicit_seats_and_configured_default(self) -> None:
        self.request = dataclasses.replace(self.request, raw_arguments="--panel full --seats opus requirements.md")
        step = self.draft()
        self.assertEqual([item.review_item.seat for item in step.work_items], ["opus"])
        mt.cancel_measure_twice(step.ref, "another isolated selection")
        loop_state.resolve("mt", "isolated").unlink()
        (self.root / ".crew" / "config.toml").write_text('default_panel="solo"\n')
        config._reset_cache_for_tests()
        self.request = dataclasses.replace(self.request, raw_arguments="requirements.md")
        default = self.draft()
        self.assertEqual([item.review_item.seat for item in default.work_items], ["opus"])
        self.assertIsNone(mt.journal_from_dict(self.state()["mt_workflow"]).selection.panel)


    def test_runtime_cleans_launched_handles_on_admission_launch_and_cancel_races(self) -> None:
        for failure in ("admission", "launch", "cancel", "duplicate"):
            with self.subTest(failure=failure):
                step = self.draft()
                launches: list[str] = []
                cancelled: list[str] = []
                class Runtime:
                    def launch(self, item: mt.MeasureWorkItem) -> str:
                        if launches and failure == "launch":
                            raise OSError("second launch failed")
                        launches.append(item.action_id)
                        if len(launches) == 1 and failure == "cancel":
                            mt.cancel_measure_twice(step.ref, "cancel during launch")
                        return launches[0] if failure == "duplicate" else item.action_id
                    def completions(self, handles: tuple[str, ...]):
                        raise AssertionError("admission failure must not await completions")
                    def cancel(self, handle: str) -> bool:
                        cancelled.append(handle)
                        return True
                issued = step
                if failure == "admission":
                    foreign = mt.MeasureRef("another", step.ref.loop_instance_id)
                    issued = dataclasses.replace(step, work_items=(step.work_items[0],
                        dataclasses.replace(step.work_items[1], owner=foreign)))
                with self.assertRaises((rw.WorkflowError, OSError)):
                    transport.run_measure_batch(issued, Runtime())
                self.assertEqual(cancelled, [step.work_items[0].action_id])
                if failure == "cancel":
                    self.assertEqual(self.state()["exit_kind"], "cancelled")
                else:
                    mt.cancel_measure_twice(step.ref, "next isolated failure")
                shutil.rmtree(self.root / ".crew")

    def test_runtime_cleanup_reports_unavailable_or_failed_cancellation(self) -> None:
        for cancel in (None, "refused", "error"):
            with self.subTest(cancel=cancel):
                step = self.draft()
                class Runtime:
                    def launch(self, item: mt.MeasureWorkItem) -> str:
                        if item.action_id != step.work_items[0].action_id:
                            raise OSError("second launch failed")
                        return item.action_id
                    def completions(self, handles: tuple[str, ...]):
                        return ()
                runtime = Runtime()
                if cancel is not None:
                    def cancel_handle(handle: str) -> bool:
                        if cancel == "error":
                            raise RuntimeError("runtime refused cancellation")
                        return False
                    runtime.cancel = cancel_handle
                with self.assertLogs(transport.logger, level="WARNING") as logs, self.assertRaises(OSError):
                    transport.run_measure_batch(step, runtime)
                diagnostic = "\n".join(logs.output)
                self.assertIn("unavailable" if cancel is None else "could not stop" if cancel == "refused" else "cancellation failed", diagnostic)
                mt.cancel_measure_twice(step.ref, "next isolated failure")
                shutil.rmtree(self.root / ".crew")

    def test_runtime_cancels_only_owned_pending_handles_after_owner_cancel(self) -> None:
        step = self.draft()
        cancelled: list[str] = []
        class Runtime:
            def launch(self, item: mt.MeasureWorkItem) -> str:
                return item.action_id
            def completions(self, handles: tuple[str, ...]):
                mt.cancel_measure_twice(step.ref, "test runtime owner cancellation")
                yield transport.Completion(handles[0], VALID)
            def cancel(self, handle: str) -> bool:
                cancelled.append(handle)
                return True
        with self.assertRaises(rw.WorkflowError):
            transport.run_measure_batch(step, Runtime())
        self.assertEqual(cancelled, [step.work_items[1].action_id])
        self.assertEqual(self.state()["exit_kind"], "cancelled")


    def test_deterministic_trace_matches_baseline_inputs_and_has_no_extra_paid_work(self) -> None:
        from measure_twice_trace import trace
        baseline = json.loads((Path(__file__).parent / "fixtures" / "measure_twice_baseline_trace.json").read_text())
        expected = json.loads((Path(__file__).parent / "fixtures" / "measure_twice_migrated_trace.json").read_text())
        actual = trace()
        self.assertEqual(actual, expected)
        self.assertEqual(actual["target"], baseline["target"])
        self.assertEqual(actual["panel"], baseline["panel"])
        for role in ("advisor", "reviewer", "formatter", "parent_synthesis"):
            self.assertEqual(actual["counts"][role], baseline["counts"][role])
        self.assertEqual(actual["counts"]["scribe"], 0)
        self.assertEqual(actual["terminal_replay_paid_work"], 0)
        self.assertIsNone(actual["live_costs"])

    def test_actual_standalone_debate_and_build_prep_do_not_select_loop_evidence(self) -> None:
        panel = self.draft()
        ref = panel.work_items[0].review_ref
        debate = rw.start_debate(rw.DebateRequest("a harmless question", seats="opus", session_id="isolated"))
        prep = self.cli("review-prep", str(self.root / "requirements.md"), "--seats", "opus", "--session-id", "isolated")
        self.assertEqual(prep.returncode, 0, prep.stdout + prep.stderr)
        directory = self.root / ".crew" / "reviews" / "isolated"
        pointer = (directory / "current-run.json").read_bytes()
        self.assertNotEqual(json.loads(pointer)["run_id"], ref.run_id)
        self.assertEqual(self.panel(panel).outcome["status"], "approved")
        self.assertEqual((directory / "current-run.json").read_bytes(), pointer)
        self.assertEqual(rw.next_review(debate.ref).ref, debate.ref)

    def test_review_prepare_after_run_write_before_bind_reopens_same_checkpoint(self) -> None:
        step = self.start()
        item = step.work_items[0]
        mt.claim_measure_action(step.ref, item.action_id)
        Path(item.staging_path).write_bytes(b"# Plan")
        original = rw._start_loop_review_under_owner_lock
        calls = 0
        def crash(prepared: rw.PreparedLoopReview) -> rw.ReviewStep:
            nonlocal calls
            calls += 1
            original(prepared)
            raise OSError("crash after run creation before bind")
        with mock.patch.object(rw, "_start_loop_review_under_owner_lock", side_effect=crash), self.assertRaises(OSError):
            transport.capture_measure_return(step.ref, item.action_id, b"done")
        journal = mt.journal_from_dict(self.state()["mt_workflow"])
        prepared = journal.pending_review_inputs
        self.assertIsNone(journal.review_ref)
        self.assertIn((prepared.ref.session_segment, prepared.ref.run_id), artifact_prune.live_run_keys(self.root / ".crew"))
        (self.root / ".crew" / "config.toml").write_text('default_panel="full"\n[seats.opus]\nmodel="sonnet"\n')
        config._reset_cache_for_tests()
        again = mt.next_measure_twice(step.ref)
        self.assertEqual(again.work_items[0].review_ref, prepared.ref)
        self.assertEqual([item.model for item in again.work_items], ["opus", "sonnet"])
        self.assertEqual(calls, 1)
        self.assertEqual(rw.start_loop_review(prepared).ref, again.work_items[0].review_ref)
        mt.cancel_measure_twice(step.ref, "stale checkpoint")
        with self.assertRaises(rw.WorkflowError):
            rw.start_loop_review(prepared)


    def test_active_advisor_cannot_be_claimed_on_an_unsupported_host(self) -> None:
        step = self.start()
        path = loop_state.resolve("mt", "isolated")
        before = path.read_bytes()
        with mock.patch.dict(os.environ, {"CREW_HOST": "codex"}), self.assertRaises(rw.WorkflowError) as caught:
            mt.claim_measure_action(step.ref, step.work_items[0].action_id)
        self.assertEqual(caught.exception.code, "unsupported_planning_host")
        self.assertEqual(path.read_bytes(), before)

    def test_document_requirements_freeze_exact_utf8_line_endings(self) -> None:
        body = b"Use exact bytes.\r\nKeep line endings.\r\n"
        (self.root / "requirements.md").write_bytes(body)
        self.start()
        requirements = mt.journal_from_dict(self.state()["mt_workflow"]).requirements
        self.assertEqual(requirements.content.encode(), body)
        self.assertEqual(requirements.sha256, mt.sha256(body))


    def test_partial_panel_retry_reissues_only_failed_seat_and_preserves_success(self) -> None:
        initial = self.draft()
        wait = self.panel(initial, failed=1)
        retry = mt.decide_measure_twice(wait.ref, mt.MeasureDecision(wait.ref, wait.question.question_id, "retry_review"))
        self.assertEqual([item.review_item.seat for item in retry.work_items], ["opus"])
        self.assertEqual(retry.work_items[0].review_ref.attempt_id, "attempt-0002")
        terminal = self.panel(retry)
        self.assertEqual(terminal.outcome["status"], "approved")
        self.assertEqual(self.state()["consecutive_review_failures"], 0)

    def test_successful_review_breaks_consecutive_all_failed_evaluations(self) -> None:
        initial = self.draft()
        fresh = self.panel(initial, failed=2)
        self.assertEqual(fresh.work_items[0].review_ref.target_sha256, initial.work_items[0].review_ref.target_sha256)
        self.assertEqual(self.state()["consecutive_review_failures"], 1)
        revise = self.panel(fresh, "REVISE", failed=1)
        self.assertEqual(revise.work_items[0].kind, "advisor")
        self.assertEqual(self.state()["consecutive_review_failures"], 0)
        self.assertEqual(self.state()["revision_round"], 1)

    def test_prior_legacy_verdict_uses_verified_panel_for_structural_action(self) -> None:
        for verdict, stage in (("REVISE", "revision"), ("REJECT", "replanning")):
            plan = self.root / "legacy.md"
            plan.write_bytes(b"# Original legacy plan")
            result = self.cli("review-prep", str(plan), "--seats", "opus", "--session-id", "isolated")
            self.assertEqual(result.returncode, 0, result.stderr)
            prep = json.loads(result.stdout)
            returned = self.root / "legacy-review.txt"
            returned.write_bytes(VALID)
            persist = self.cli("persist-seat", "opus", "--session-id", "isolated", "--run-id", prep["run_id"],
                "--model", "opus", "-f", str(returned))
            self.assertEqual(persist.returncode, 0, persist.stderr)
            run = Path(prep["run_dir"])
            collect = self.cli("collect", "--session-id", "isolated", "--run-id", prep["run_id"], "--seats", "opus",
                "--group", "-o", str(run / "panel.md"), "--full", str(run / "panel-full.md"))
            self.assertEqual(collect.returncode, 0, collect.stderr)
            record = rw.review_runs.read_run_json(run)
            models.atomic_write_json(loop_state.resolve("mt", "isolated"), {
                "schema": 3, "loop": "mt", "active": True, "session_id": "isolated", "loop_instance_id": "legacy",
                "task": "--seats opus preserved legacy task", "plan_file": str(plan), "phase": "drafting",
                "run_id": record["run_id"], "target_sha256": record["target_sha256"],
                "target_spec": record["target_spec"], "target_base": "", "expected_seats": ["opus"],
                "last_verdict": verdict, "revision_round": 3, "stop_fires": 11})
            wait = self.start()
            next_step = self.confirm_legacy(wait)
            self.assertEqual(next_step.work_items[0].kind, "advisor")
            journal = mt.journal_from_dict(self.state()["mt_workflow"])
            self.assertEqual(journal.stage, stage)
            self.assertEqual(journal.feedback_path, str(run / "panel-full.md"))
            self.assertEqual(journal.review_generation, 0)
            self.assertEqual(self.state()["revision_round"], 3)
            self.assertEqual(self.state()["stop_fires"], 11)
            loop_state.resolve("mt", "isolated").unlink()
            shutil.rmtree(self.root / ".crew" / "plans")
            shutil.rmtree(self.root / ".crew" / "reviews")

    def test_replacement_lifetime_and_other_session_cannot_adopt_review_result(self) -> None:
        step = self.draft()
        item = step.work_items[0]
        rw.claim_review_action(rw.ClaimRequest(item.review_ref, item.action_id))
        mt.cancel_measure_twice(step.ref, "replace")
        init = self.cli("state", "init", "mt", "--auto-plan", "--task", self.request.raw_arguments, "--session-id", "isolated")
        self.assertEqual(init.returncode, 0, init.stderr)
        replacement = self.start()
        self.assertNotEqual(replacement.ref, step.ref)
        before = loop_state.resolve("mt", "isolated").read_bytes()
        with self.assertRaises(rw.WorkflowError):
            transport.capture_review_return(item.review_ref, item.action_id, VALID)
        wrong = dataclasses.replace(item.review_ref, session_segment="other-session")
        with self.assertRaises(rw.WorkflowError):
            transport.capture_review_return(wrong, item.action_id, VALID)
        self.assertEqual(loop_state.resolve("mt", "isolated").read_bytes(), before)


    def test_human_retry_of_drifted_target_creates_fresh_evaluation(self) -> None:
        initial = self.draft()
        Path(self.state()["plan_file"]).write_bytes(b"# Human edited plan")
        wait = self.panel(initial)
        self.assertIn("target-drift", wait.question.advisories)
        self.assertEqual(len(wait.question.outcome_sha256), 64)
        fresh = mt.decide_measure_twice(wait.ref, mt.MeasureDecision(wait.ref, wait.question.question_id, "retry_review"))
        old_ref = initial.work_items[0].review_ref
        new_ref = fresh.work_items[0].review_ref
        self.assertNotEqual(new_ref.target_sha256, old_ref.target_sha256)
        self.assertNotEqual(new_ref.run_id, old_ref.run_id)
        self.assertEqual(self.state()["revision_round"], 0)
        self.assertEqual(self.panel(fresh).outcome["status"], "approved")

    def test_human_retry_of_missing_target_parks_before_new_planning(self) -> None:
        initial = self.draft()
        Path(self.state()["plan_file"]).unlink()
        wait = self.panel(initial)
        parked = mt.decide_measure_twice(wait.ref, mt.MeasureDecision(wait.ref, wait.question.question_id, "retry_review"))
        self.assertEqual(parked.question.kind, "plan_recovery")
        self.assertEqual(self.state()["last_verdict"], "")
        fresh = mt.decide_measure_twice(parked.ref, mt.MeasureDecision(parked.ref, parked.question.question_id, "retry_advisor"))
        self.assertEqual(fresh.work_items[0].kind, "advisor")
        self.assertEqual(self.state()["phase"], "drafting")
        self.assertIsNone(mt.journal_from_dict(self.state()["mt_workflow"]).review_ref)


if __name__ == "__main__":
    unittest.main()
