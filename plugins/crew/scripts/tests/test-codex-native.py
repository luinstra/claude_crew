"""Codex transport contracts in disposable roots, with no model invocation."""

from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import loop_state
from multiagent import build_workflow as build
from multiagent import channels, cli, config, execution, seats
from multiagent import codex_native_transport as native
from multiagent import measure_twice as measure
from multiagent import review_workflow as review
from multiagent import workflow_transport as transport

VALID = b"## VERDICT\nAPPROVED\n\n## FINDINGS\nnone\n"
COMPLETED = b"Implemented\r\nCREW_BUILD_STATUS: COMPLETED"
PLAN = b"# Plan\r\nWrite hello.txt and verify its contents."


class CodexNativeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.session = "codex-test-" + uuid.uuid4().hex
        environment = mock.patch.dict(
            os.environ,
            {
                "HOME": str(self.home),
                "CREW_HOST": "codex",
                "CREW_PROJECT_DIR": "",
                "CLAUDE_PROJECT_DIR": str(self.root),
                "CLAUDE_WORKING_DIRECTORY": str(self.root),
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        home = mock.patch("pathlib.Path.home", return_value=self.home)
        home.start()
        self.addCleanup(home.stop)
        config._reset_cache_for_tests()
        self.addCleanup(config._reset_cache_for_tests)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / ".gitignore").write_text(".crew/\nhome/\n")
        self.plan = self.root / "requirements.md"
        self.plan.write_bytes(PLAN)

    def start_review(self, roster: str = "sol,luna") -> review.ReviewStep:
        return review.start_review(
            review.ReviewRequest(
                str(self.plan),
                seats=roster,
                session_id=self.session,
                timeout_seconds=1,
            )
        )

    def bind_review(
        self, ref: review.ReviewRef, item: review.WorkItem, handle: str
    ) -> None:
        claim = review.claim_review_action(review.ClaimRequest(ref, item.action_id))
        self.assertEqual(claim.authorization, "spawn")
        self.assertEqual(claim.work_item, item)
        native.bind_native_launch(native.NativeLaunch(ref, item.action_id, handle))

    def capture_review(
        self,
        ref: review.ReviewRef,
        item: review.WorkItem,
        handle: str,
        content: bytes = VALID,
    ) -> review.ReviewStep:
        return transport.capture_review_return(
            ref, item.action_id, content, handle=handle, completion_observed=True
        )

    def panel(
        self, step: build.BuildStep | measure.MeasureStep, verdict: str = "APPROVED"
    ) -> build.BuildStep | measure.MeasureStep:
        for ordinal, item in enumerate(step.work_items):
            handle = f"review-{item.action_id.replace(':', '-')}-{ordinal}"
            self.bind_review(item.review_ref, item.review_item, handle)
            self.capture_review(item.review_ref, item.review_item, handle)
        advance = (
            build.next_build
            if isinstance(step, build.BuildStep)
            else measure.next_measure_twice
        )
        synthesis_step = advance(step.ref)
        item = synthesis_step.work_items[0]
        self.assertEqual((item.driver, item.kind), ("parent", "synthesis"))
        review.claim_review_action(review.ClaimRequest(item.review_ref, item.action_id))
        count = 1 if verdict == "REVISE" else 0
        transport.capture_review_return(
            item.review_ref,
            item.action_id,
            f"Full synthesis\nBLOCKING_CAUSES: {count}\n".encode(),
            judgment=transport.SynthesisJudgment(verdict),
        )
        return advance(step.ref)

    def start_build(self, executor: str = "crew:executor") -> build.BuildStep:
        return build.start_build(
            build.BuildRequest(
                f"--seats sol,luna --executor {executor} Write hello.txt",
                self.session,
            )
        )

    def claim_writer(
        self, step: build.BuildStep, handle: str = "writer"
    ) -> build.BuildWorkItem:
        item = step.work_items[0]
        self.assertEqual(
            build.claim_build_action(step.ref, item.action_id)["authorization"], "spawn"
        )
        build.bind_build_native(step.ref, item.action_id, handle)
        return item

    def build_state(self) -> dict:
        return loop_state.read(loop_state.resolve("bl", self.session))

    def measure_state(self) -> dict:
        return loop_state.read(loop_state.resolve("mt", self.session))

    def start_measure(self) -> measure.MeasureStep:
        return measure.start_measure_twice(
            measure.MeasureRequest(
                "--seats sol,luna requirements.md",
                self.session,
            )
        )

    def cli_call(self, argv: list[str]) -> tuple[int, dict]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cli.main(argv)
        return code, json.loads(output.getvalue())

    def test_native_review_uses_exact_frozen_model_effort_and_fresh_history(
        self,
    ) -> None:
        directory = self.root / ".crew"
        directory.mkdir(exist_ok=True)
        settings = directory / "config.toml"
        settings.write_text(
            '[seats.sol]\nmodel = "gpt-6-sol"\nreasoning_effort = "high"\n'
        )
        config._reset_cache_for_tests()
        step = self.start_review()
        item = step.work_items[0]
        spawn = item.native_transport["spawn"]
        self.assertEqual(
            (item.driver, item.access, item.channel),
            ("native", "read-only-advisory", "codex"),
        )
        self.assertEqual(
            (spawn["model"], spawn["reasoning_effort"], spawn["fork_turns"]),
            ("gpt-6-sol", "high", "none"),
        )
        self.assertEqual(
            set(spawn),
            {"task_name", "message", "model", "reasoning_effort", "fork_turns"},
        )
        self.assertIn("Adapter role: crew:reviewer", spawn["message"])
        self.assertIn("inherit the parent's sandbox and tools", spawn["message"])
        self.assertIn(str(Path(item.prompt_path)), spawn["message"])
        self.assertEqual(item.return_transport["primary"]["kind"], "host_write")
        self.assertNotIn("native_capture", item.commands)
        self.assertIn("native_bind", item.commands)
        self.assertEqual(
            review.parse_review_step(review.review_step_to_dict(step)), step
        )
        self.assertNotEqual(
            spawn["task_name"],
            step.work_items[1].native_transport["spawn"]["task_name"],
        )
        settings.write_text(
            '[seats.sol]\nmodel = "changed"\nreasoning_effort = "low"\n'
        )
        config._reset_cache_for_tests()
        self.assertEqual(
            review.next_review(step.ref).work_items[0].native_transport,
            item.native_transport,
        )

    def set_sol_effort(self, effort: str) -> None:
        directory = self.root / ".crew"
        directory.mkdir(exist_ok=True)
        (directory / "config.toml").write_text(
            f'[seats.sol]\nreasoning_effort = "{effort}"\n'
        )
        config._reset_cache_for_tests()

    def test_invalid_native_effort_is_rejected_before_review_freeze(self) -> None:
        self.set_sol_effort("minimal")
        with self.assertRaises(review.WorkflowError) as refused:
            self.start_review("sol")
        self.assertEqual(refused.exception.code, "unsupported_native_effort")
        self.assertFalse(list((self.root / ".crew").glob("reviews/**/run.json")))
        self.set_sol_effort("low")
        step = self.start_review("sol")
        self.assertEqual(
            step.work_items[0].native_transport["spawn"]["reasoning_effort"], "low"
        )

    def test_build_resumes_after_invalid_review_effort_is_corrected(self) -> None:
        step = self.start_build()
        item = self.claim_writer(step)
        self.set_sol_effort("minimal")
        with self.assertRaises(review.WorkflowError) as refused:
            build.capture_build_return(
                step.ref,
                item.action_id,
                COMPLETED,
                handle="writer",
                completion_observed=True,
            )
        self.assertEqual(refused.exception.code, "unsupported_native_effort")
        state = self.build_state()["bl_workflow"]
        self.assertIsNone(state["pending_review_inputs"])
        self.assertIsNone(state["outstanding_writer"])
        self.set_sol_effort("low")
        reviewers = build.next_build(step.ref)
        self.assertEqual(
            reviewers.work_items[0].review_item.native_transport["spawn"][
                "reasoning_effort"
            ],
            "low",
        )
        self.assertEqual(self.panel(reviewers).outcome["status"], "approved")

    def test_planning_resumes_after_invalid_review_effort_is_corrected(self) -> None:
        step = self.start_measure()
        item = step.work_items[0]
        measure.claim_measure_action(step.ref, item.action_id)
        native.bind_native_launch(
            native.NativeLaunch(step.ref, item.action_id, "advisor")
        )
        Path(item.staging_path).write_bytes(PLAN)
        self.set_sol_effort("minimal")
        with self.assertRaises(review.WorkflowError) as refused:
            transport.capture_measure_return(
                step.ref,
                item.action_id,
                b"Plan written",
                handle="advisor",
                completion_observed=True,
            )
        self.assertEqual(refused.exception.code, "unsupported_native_effort")
        self.assertIsNone(self.measure_state()["mt_workflow"]["pending_review_inputs"])
        self.set_sol_effort("low")
        reviewers = measure.next_measure_twice(step.ref)
        self.assertEqual(
            reviewers.work_items[0].review_item.native_transport["spawn"][
                "reasoning_effort"
            ],
            "low",
        )
        self.assertEqual(self.panel(reviewers).outcome["status"], "approved")

    def test_review_and_debate_reject_model_inheritance_before_freeze(self) -> None:
        directory = self.root / ".crew"
        directory.mkdir(exist_ok=True)
        settings = directory / "config.toml"
        settings.write_text('[seats.sol]\nmodel = "inherit"\n')
        config._reset_cache_for_tests()
        for mode in ("review", "debate"):
            with self.subTest(mode=mode):
                with self.assertRaises(review.WorkflowError) as refused:
                    if mode == "review":
                        self.start_review("sol")
                    else:
                        review.start_debate(
                            review.DebateRequest(
                                "Ship it?",
                                seats="sol",
                                session_id=self.session,
                            )
                        )
                self.assertEqual(refused.exception.code, "unsupported_native_model")
                self.assertFalse(list(directory.glob("reviews/**/run.json")))
        settings.write_text('[seats.sol]\nmodel = "gpt-6-sol"\n')
        config._reset_cache_for_tests()
        item = self.start_review("sol").work_items[0]
        self.assertEqual(item.native_transport["spawn"]["model"], "gpt-6-sol")

    def test_loop_review_preparation_rejects_model_inheritance(self) -> None:
        directory = self.root / ".crew"
        directory.mkdir(exist_ok=True)
        (directory / "config.toml").write_text('[seats.sol]\nmodel = "inherit"\n')
        config._reset_cache_for_tests()
        for loop in ("bl", "mt"):
            with self.subTest(loop=loop):
                binding = review.LoopReviewBinding(
                    self.session, loop, str(uuid.uuid4()), 1
                )
                request = review.ReviewRequest(
                    "working-tree" if loop == "bl" else str(self.plan),
                    seats="sol",
                    session_id=self.session,
                )
                with self.assertRaises(review.WorkflowError) as refused:
                    review.prepare_loop_review(
                        request, binding, executor_summary="Completed"
                    )
                self.assertEqual(refused.exception.code, "unsupported_native_model")
                self.assertFalse(list(directory.glob("reviews/**/run.json")))

    def test_review_launch_metadata_never_omits_model(self) -> None:
        ref = review.ReviewRef(
            self.session, "run-123456789abc", "attempt-0001", "0" * 64
        )
        for role in ("crew:reviewer", "crew:panelist"):
            for model in (None, "inherit"):
                with self.subTest(role=role, model=model):
                    with self.assertRaises(review.WorkflowError) as refused:
                        native.launch_metadata(
                            ref,
                            "action-0001",
                            role,
                            model,
                            None,
                            str(self.plan),
                            str(self.root / "return.md"),
                        )
                    self.assertEqual(refused.exception.code, "unsupported_native_model")

    def test_force_external_does_not_change_named_executor_selection(self) -> None:
        self.assertEqual(channels.native_channel("codex"), "codex")
        self.assertIsNone(channels.task_native_channel("codex"))
        chosen = execution.resolve_executor(self.session, "sol")
        self.assertEqual((chosen.executor, chosen.channel), ("sol", "codex"))
        step = self.start_build("sol")
        self.assertEqual(
            (step.work_items[0].driver, step.work_items[0].native_transport),
            ("external", None),
        )
        forced = review.start_review(
            review.ReviewRequest(
                str(self.plan),
                seats="sol",
                session_id=self.session + "-forced",
                force_external_channels=("codex",),
            )
        )
        self.assertEqual(
            (forced.work_items[0].driver, forced.work_items[0].access),
            ("external", "read-only"),
        )
        self.assertIsNone(forced.work_items[0].native_transport)

    def test_debate_uses_panelist_prompt_and_exact_pins(self) -> None:
        step = review.start_debate(
            review.DebateRequest(
                "Should we ship?",
                seats="sol,luna",
                session_id=self.session,
                timeout_seconds=1,
            )
        )
        for ordinal, item in enumerate(step.work_items):
            self.assertEqual(item.role, "crew:panelist")
            self.assertIn("# panelist", item.native_transport["spawn"]["message"])
            self.assertEqual(
                item.native_transport["spawn"]["model"],
                seats.seat_spec(item.seat).model,
            )
            handle = f"panelist-{ordinal}"
            self.bind_review(step.ref, item, handle)
            result = self.capture_review(
                step.ref, item, handle, b"My independent position."
            )
        self.assertEqual(result.work_items[0].kind, "synthesis")
        self.assertEqual(result.work_items[0].driver, "parent")

    def test_claim_binding_wrong_handle_duplicate_and_capture_bytes(self) -> None:
        step = self.start_review("sol")
        item = step.work_items[0]
        with self.assertRaises(review.WorkflowError):
            native.bind_native_launch(
                native.NativeLaunch(step.ref, item.action_id, "unclaimed")
            )
        self.bind_review(step.ref, item, "actual")
        native.bind_native_launch(
            native.NativeLaunch(step.ref, item.action_id, "actual")
        )
        with self.assertRaises(review.WorkflowError):
            native.bind_native_launch(
                native.NativeLaunch(step.ref, item.action_id, "different")
            )
        with self.assertRaises(review.WorkflowError):
            self.capture_review(step.ref, item, "wrong")
        with self.assertRaises(review.WorkflowError) as missing:
            transport.capture_review_return(
                step.ref, item.action_id, VALID, handle="actual"
            )
        self.assertEqual(missing.exception.code, "completion_not_observed")
        raw = VALID.replace(b"\n", b"\r\n").rstrip(b"\r\n")
        self.capture_review(step.ref, item, "actual", raw)
        run = Path(item.prompt_path).parents[3]
        accepted = list(run.glob("**/*"))
        self.assertTrue(
            any(path.is_file() and path.read_bytes() == raw for path in accepted)
        )
        self.capture_review(step.ref, item, "actual", raw)
        with self.assertRaises(review.WorkflowError):
            self.capture_review(step.ref, item, "actual", raw + b"\n")
        self.assertEqual(
            review.claim_review_action(
                review.ClaimRequest(step.ref, item.action_id)
            ).authorization,
            "do_not_spawn",
        )

    def test_one_handle_cannot_be_bound_to_two_seats(self) -> None:
        step = self.start_review()
        self.bind_review(step.ref, step.work_items[0], "shared-handle")
        other = step.work_items[1]
        review.claim_review_action(review.ClaimRequest(step.ref, other.action_id))
        with self.assertRaises(review.WorkflowError) as duplicate:
            native.bind_native_launch(
                native.NativeLaunch(step.ref, other.action_id, "shared-handle")
            )
        self.assertEqual(duplicate.exception.code, "conflict")

    def test_native_raw_reviewer_uses_parent_formatter(self) -> None:
        step = self.start_review("sol")
        item = step.work_items[0]
        self.bind_review(step.ref, item, "raw-reviewer")
        formatted = self.capture_review(
            step.ref, item, "raw-reviewer", b"Unstructured evidence."
        )
        formatter = formatted.work_items[0]
        self.assertEqual(
            (formatter.kind, formatter.driver, formatter.access),
            ("formatter", "parent", "parent-context"),
        )
        self.assertIsNone(formatter.native_transport)

    def test_manual_native_submission_cannot_bypass_capture_binding(self) -> None:
        step = self.start_review("sol")
        item = step.work_items[0]
        review.claim_review_action(review.ClaimRequest(step.ref, item.action_id))
        artifact = Path(item.native_transport["returned_path"])
        artifact.write_bytes(VALID)
        result = review.HostResult(
            step.ref,
            item.action_id,
            "ok",
            {"path": str(artifact), "sha256": measure.sha256(VALID)},
            None,
            None,
        )
        submission = Path(item.submission_path)
        submission.write_text(json.dumps(review.host_result_to_dict(result)))
        with self.assertRaises(review.WorkflowError) as refused:
            review.submit_review(
                review.SubmissionRequest(str(submission), True, result)
            )
        self.assertEqual(refused.exception.code, "invalid_submission")
        self.assertTrue(submission.is_file())

    def test_runtime_duplicate_callback_is_rejected_after_one_receipt(self) -> None:
        step = self.start_build()

        class Runtime:
            def launch(self, item: build.BuildWorkItem) -> str:
                return "runtime-writer"

            def completions(
                self, handles: tuple[str, ...]
            ) -> tuple[transport.Completion, ...]:
                result = transport.Completion("runtime-writer", COMPLETED)
                return result, result

        with self.assertRaises(review.WorkflowError):
            transport.run_build_batch(step, Runtime(), max_concurrency=1)
        state = self.build_state()["bl_workflow"]
        self.assertEqual(len(state["accepted_actions"]), 1)
        self.assertIsNone(state["outstanding_writer"])

    def test_skills_are_discoverable_source_adapters_with_relative_roots(self) -> None:
        plugin = Path(__file__).resolve().parents[2]
        for name in ("review", "debate", "measure-twice", "build"):
            skill = plugin / "skills-codex" / name / "SKILL.md"
            text = skill.read_text()
            self.assertIn(f"name: {name}\n", text)
            self.assertIn(f"commands/{name}.md", text)
            self.assertIn("docs/codex-transport.md", text)
            self.assertNotIn("CLAUDE_PLUGIN_ROOT", text)
            self.assertTrue((skill.parent / "../.." / "crew").resolve().is_file())

    def test_codex_manifest_scopes_skills_away_from_other_hosts(self) -> None:
        plugin = Path(__file__).resolve().parents[2]
        codex = json.loads((plugin / ".codex-plugin/plugin.json").read_text())
        claude = json.loads((plugin / ".claude-plugin/plugin.json").read_text())
        cursor = json.loads((plugin / ".cursor-plugin/plugin.json").read_text())
        self.assertEqual(
            (codex["name"], codex["version"]), (claude["name"], claude["version"])
        )
        self.assertEqual(codex["skills"], "./skills-codex/")
        self.assertEqual(codex["hooks"], "./hooks/hooks.json")
        self.assertNotIn("skills", claude)
        self.assertNotIn("skills", cursor)
        self.assertFalse((plugin / "skills").exists())
        expected = {
            "review",
            "debate",
            "measure-twice",
            "build",
            "cancel-build",
            "cancel-measure-twice",
            "save-context",
            "restore-context",
        }
        discovered = {
            p.parent.name for p in (plugin / codex["skills"]).glob("*/SKILL.md")
        }
        self.assertEqual(discovered, expected)
        for name in expected:
            self.assertTrue((plugin / "commands" / f"{name}.md").is_file())

    def test_exact_model_refusal_is_failed_without_cli_fallback(self) -> None:
        step = self.start_review("sol")
        item = step.work_items[0]
        review.claim_review_action(review.ClaimRequest(step.ref, item.action_id))
        with mock.patch.object(review, "get_provider_for_channel") as provider:
            with self.assertRaises(review.WorkflowError):
                transport.capture_review_return(
                    step.ref,
                    item.action_id,
                    b"",
                    status="failed",
                    diagnostic="model unavailable",
                )
            failed = transport.capture_review_return(
                step.ref,
                item.action_id,
                b"",
                status="failed",
                diagnostic="requested model refused before launch",
                launch_refused=True,
            )
            self.assertEqual(failed.outcome["status"], "all_failed")
            provider.assert_not_called()

    def test_bound_agent_cannot_be_reported_as_a_refused_launch(self) -> None:
        step = self.start_review("sol")
        item = step.work_items[0]
        self.bind_review(step.ref, item, "running")
        with self.assertRaises(review.WorkflowError):
            transport.capture_review_return(
                step.ref,
                item.action_id,
                b"",
                status="failed",
                diagnostic="refused",
                launch_refused=True,
            )
        self.assertEqual(review.next_review(step.ref).type, "waiting")

    def test_late_reviewer_after_recovery_cannot_replace_result(self) -> None:
        step = self.start_review("sol")
        item = step.work_items[0]
        self.bind_review(step.ref, item, "old")
        review.recover_review_action(
            review.RecoveryRequest(
                step.ref, item.action_id, "not_running", "native_task_lost"
            )
        )
        with self.assertRaises(review.WorkflowError):
            self.capture_review(step.ref, item, "old")
        self.assertEqual(review.next_review(step.ref).outcome["status"], "all_failed")

    def test_binding_symlink_is_refused_without_outside_write(self) -> None:
        step = self.start_review("sol")
        item = step.work_items[0]
        self.bind_review(step.ref, item, "actual")
        binding = next(
            (Path(item.prompt_path).parents[1] / "native-transport").glob(
                "*.launch.json"
            )
        )
        sentinel = self.root / "outside.json"
        sentinel.write_bytes(binding.read_bytes())
        binding.unlink()
        binding.symlink_to(sentinel)
        before = sentinel.read_bytes()
        with self.assertRaises(review.WorkflowError):
            self.capture_review(step.ref, item, "actual")
        self.assertEqual(sentinel.read_bytes(), before)

    def test_advisor_stages_promotes_and_completes_with_native_reviews(self) -> None:
        step = self.start_measure()
        item = step.work_items[0]
        claimed = measure.claim_measure_action(step.ref, item.action_id)
        self.assertEqual(measure.parse_measure_work(claimed["work_item"]), item)
        spawn = claimed["work_item"]["native_transport"]["spawn"]
        self.assertNotIn("model", spawn)
        self.assertEqual(spawn["fork_turns"], "none")
        self.assertIn("crew:advisor", spawn["message"])
        self.assertNotIn(
            "native_transport", self.measure_state()["mt_workflow"]["action"]["item"]
        )
        native.bind_native_launch(
            native.NativeLaunch(step.ref, item.action_id, "advisor")
        )
        Path(item.staging_path).write_bytes(PLAN)
        raw = b"Plan written\r\nNo implementation."
        reviewers = transport.capture_measure_return(
            step.ref, item.action_id, raw, handle="advisor", completion_observed=True
        )
        canonical = Path(self.measure_state()["plan_file"])
        self.assertEqual(canonical.read_bytes(), PLAN)
        self.assertNotEqual(canonical, Path(item.staging_path))
        Path(item.staging_path).write_bytes(b"changed staging")
        transport.capture_measure_return(
            step.ref, item.action_id, raw, handle="advisor", completion_observed=True
        )
        self.assertEqual(canonical.read_bytes(), PLAN)
        code, resumed = self.cli_call(
            ["measure-twice-resume", "--session-id", self.session]
        )
        self.assertEqual((code, resumed["ref"]), (0, measure.ref_to_dict(step.ref)))
        done = self.panel(reviewers)
        self.assertEqual(done.outcome["status"], "approved")
        code, resumed = self.cli_call(
            ["measure-twice-resume", "--session-id", self.session]
        )
        self.assertEqual((code, resumed), (0, measure.step_to_dict(done)))

    def test_advisor_wrong_handle_and_missing_plan_cannot_promote(self) -> None:
        step = self.start_measure()
        item = step.work_items[0]
        measure.claim_measure_action(step.ref, item.action_id)
        native.bind_native_launch(
            native.NativeLaunch(step.ref, item.action_id, "advisor")
        )
        with self.assertRaises(review.WorkflowError):
            transport.capture_measure_return(
                step.ref,
                item.action_id,
                b"done",
                handle="wrong",
                completion_observed=True,
            )
        failed = transport.capture_measure_return(
            step.ref,
            item.action_id,
            b"done",
            handle="advisor",
            completion_observed=True,
        )
        self.assertEqual(failed.question.kind, "advisor_retry")
        self.assertFalse(self.measure_state().get("plan_file"))

    def test_advisor_cancelled_owner_rejects_late_return(self) -> None:
        step = self.start_measure()
        item = step.work_items[0]
        measure.claim_measure_action(step.ref, item.action_id)
        native.bind_native_launch(
            native.NativeLaunch(step.ref, item.action_id, "advisor")
        )
        measure.cancel_measure_twice(step.ref, "cancel")
        with self.assertRaises(review.WorkflowError):
            transport.capture_measure_return(
                step.ref,
                item.action_id,
                b"late",
                handle="advisor",
                completion_observed=True,
            )
        self.assertFalse(self.measure_state().get("plan_file"))

    def test_build_revision_is_fresh_and_explicit_resume_replays_terminal(self) -> None:
        step = self.start_build()
        item = self.claim_writer(step)
        journal = build.journal_from_dict(self.build_state()["bl_workflow"])
        self.assertEqual(
            (
                journal.executor.host,
                journal.executor.channel,
                journal.executor.resume_executor,
            ),
            ("codex", "codex", False),
        )
        self.assertNotIn("model", item.native_transport["spawn"])
        (self.root / "hello.txt").write_text("hello\n")
        review_step = build.capture_build_return(
            step.ref,
            item.action_id,
            COMPLETED,
            handle="writer",
            completion_observed=True,
        )
        self.assertEqual(build.resume_build(self.session), review_step)
        revised = self.panel(review_step, "REVISE")
        second = self.claim_writer(revised, "revision-writer")
        self.assertNotEqual(
            item.native_transport["spawn"]["task_name"],
            second.native_transport["spawn"]["task_name"],
        )
        self.assertEqual(second.native_transport["continuation"], "fresh")
        self.assertIn(
            "Full synthesis",
            Path(second.prompt_path).read_text()
            + "".join(
                Path(path).read_text()
                for path in build.journal_from_dict(
                    self.build_state()["bl_workflow"]
                ).feedback_paths
            ),
        )
        reviewed = build.capture_build_return(
            step.ref,
            second.action_id,
            COMPLETED,
            handle="revision-writer",
            completion_observed=True,
        )
        done = self.panel(reviewed)
        self.assertEqual(done.outcome["status"], "approved")
        self.assertEqual(build.resume_build(self.session), done)

    def test_build_handle_guards_wrong_action_and_exact_duplicate(self) -> None:
        step = self.start_build()
        item = self.claim_writer(step)
        for action, handle, observed in (
            ("action-9999", "writer", True),
            (item.action_id, "wrong", True),
            (item.action_id, "writer", False),
        ):
            with (
                self.subTest(action=action, handle=handle, observed=observed),
                self.assertRaises(review.WorkflowError),
            ):
                build.capture_build_return(
                    step.ref,
                    action,
                    COMPLETED,
                    handle=handle,
                    completion_observed=observed,
                )
        self.assertEqual(
            self.build_state()["bl_workflow"]["outstanding_writer"], item.action_id
        )
        result = build.capture_build_return(
            step.ref,
            item.action_id,
            COMPLETED,
            handle="writer",
            completion_observed=True,
        )
        self.assertEqual(
            build.capture_build_return(
                step.ref,
                item.action_id,
                COMPLETED,
                handle="writer",
                completion_observed=True,
            ),
            result,
        )
        with self.assertRaises(review.WorkflowError):
            build.capture_build_return(
                step.ref,
                item.action_id,
                COMPLETED + b"\n",
                handle="writer",
                completion_observed=True,
            )

    def test_native_build_workspace_guard_cannot_be_waived_by_completion(self) -> None:
        step = self.start_build()
        item = self.claim_writer(step)
        before = execution.observe_workspace(str(self.root))
        changed = dataclasses.replace(before, head="different-head")
        with mock.patch.object(execution, "observe_workspace", return_value=changed):
            parked = build.capture_build_return(
                step.ref,
                item.action_id,
                COMPLETED,
                handle="writer",
                completion_observed=True,
            )
        self.assertEqual(parked.question.kind, "workspace_guard")
        self.assertIsNone(self.build_state()["bl_workflow"]["review_ref"])

    def test_native_build_unknown_workspace_refuses_claim(self) -> None:
        step = self.start_build()
        with mock.patch.object(
            execution,
            "observe_workspace",
            return_value=execution.WorkspaceFacts(None, None, None),
        ):
            claimed = build.claim_build_action(step.ref, step.work_items[0].action_id)
        self.assertEqual(claimed["authorization"], "needs_input")
        self.assertIsNone(self.build_state()["bl_workflow"]["outstanding_writer"])

    def test_interrupt_previous_status_keeps_writer_fence_until_real_final(
        self,
    ) -> None:
        step = self.start_build()
        item = self.claim_writer(step, "owned-writer")

        def interrupt(handle: str) -> str:
            self.assertEqual(handle, "owned-writer")
            self.assertFalse(self.build_state()["active"])
            return "running"

        cancelled = build.cancel_build(step.ref, cancel_handle=interrupt)
        self.assertTrue(cancelled.outcome["writer_fence"])
        self.assertEqual(cancelled.outcome["owned_handle"], "owned-writer")
        self.assertEqual(
            self.build_state()["bl_workflow"]["outstanding_writer"], item.action_id
        )
        with self.assertRaises(loop_state.LoopStateError):
            self.start_build()
        ended = build.capture_build_return(
            step.ref,
            item.action_id,
            COMPLETED,
            handle="owned-writer",
            completion_observed=True,
        )
        self.assertEqual(ended.type, "terminal")
        self.assertFalse(ended.outcome["writer_fence"])
        self.assertIsNone(self.build_state()["bl_workflow"]["review_ref"])

    def test_runtime_wrong_callback_does_not_release_owned_writer(self) -> None:
        step = self.start_build()

        class Runtime:
            def launch(self, item: build.BuildWorkItem) -> str:
                return "runtime-writer"

            def completions(
                self, handles: tuple[str, ...]
            ) -> tuple[transport.Completion, ...]:
                return (transport.Completion("unowned", COMPLETED),)

            def cancel(self, handle: str) -> bool:
                return False

        with self.assertRaises(review.WorkflowError):
            transport.run_build_batch(step, Runtime(), max_concurrency=1)
        state = self.build_state()["bl_workflow"]
        self.assertEqual(state["action"]["handle"], "runtime-writer")
        self.assertEqual(state["outstanding_writer"], step.work_items[0].action_id)

    def six_seat_panel(self, loop: str) -> build.BuildStep | measure.MeasureStep:
        directory = self.root / ".crew"
        directory.mkdir(exist_ok=True)
        names = [f"native-{index}" for index in range(6)]
        (directory / "config.toml").write_text(
            "\n".join(
                f'[seats.{name}]\nvia = ["codex"]\nmodel = "gpt-6-sol"\navailable = true\n'
                for name in names
            )
        )
        config._reset_cache_for_tests()
        roster = ",".join(names)
        if loop == "bl":
            step = build.start_build(
                build.BuildRequest(
                    f"--seats {roster} --executor crew:executor Write hello.txt",
                    self.session,
                )
            )
            item = self.claim_writer(step)
            return build.capture_build_return(
                step.ref,
                item.action_id,
                COMPLETED,
                handle="writer",
                completion_observed=True,
            )
        step = measure.start_measure_twice(
            measure.MeasureRequest(
                f"--seats {roster} requirements.md",
                self.session,
            )
        )
        item = step.work_items[0]
        measure.claim_measure_action(step.ref, item.action_id)
        native.bind_native_launch(
            native.NativeLaunch(step.ref, item.action_id, "advisor")
        )
        Path(item.staging_path).write_bytes(PLAN)
        return transport.capture_measure_return(
            step.ref,
            item.action_id,
            b"Plan written",
            handle="advisor",
            completion_observed=True,
        )

    def check_capacity_bounded_panel(self, loop: str, *, partial: bool = False) -> None:
        step = self.six_seat_panel(loop)
        self.assertEqual(len(step.work_items), 6)
        launched: list[str] = []
        active: set[str] = set()
        waves: list[int] = []

        class Runtime:
            def launch(
                self, item: build.BuildWorkItem | measure.MeasureWorkItem
            ) -> str:
                if len(active) >= 3:
                    raise AssertionError("native capacity exhausted")
                handle = item.action_id
                launched.append(handle)
                active.add(handle)
                return handle

            def completions(
                self, handles: tuple[str, ...]
            ) -> tuple[transport.Completion, ...]:
                waves.append(len(handles))
                observed = handles[:1] if partial else handles
                for handle in observed:
                    active.remove(handle)
                return tuple(transport.Completion(handle, VALID) for handle in observed)

        runner = (
            transport.run_build_batch if loop == "bl" else transport.run_measure_batch
        )
        with mock.patch.object(review, "claim_review_action") as claim:
            with self.assertRaises(review.WorkflowError) as refused:
                runner(step, Runtime())
            self.assertEqual(refused.exception.code, "native_capacity_required")
            claim.assert_not_called()
        result = runner(step, Runtime(), max_concurrency=3)
        if partial:
            self.assertEqual((len(launched), len(active), waves), (3, 2, [3]))
            self.assertEqual(len(result.work_items), 3)
            self.assertEqual(runner(result, Runtime(), max_concurrency=3), result)
            self.assertEqual((len(launched), len(active), waves), (3, 2, [3]))
        else:
            self.assertEqual((len(launched), active, waves), (6, set(), [3, 3]))
            self.assertEqual(result.work_items[0].kind, "synthesis")

    def test_build_panel_runs_six_seats_in_three_slot_waves(self) -> None:
        self.check_capacity_bounded_panel("bl")

    def test_planning_panel_runs_six_seats_in_three_slot_waves(self) -> None:
        self.check_capacity_bounded_panel("mt")

    def test_incomplete_wave_leaves_deferred_seats_unclaimed(self) -> None:
        self.check_capacity_bounded_panel("bl", partial=True)

    def test_partial_planning_wave_is_not_relaunched(self) -> None:
        self.check_capacity_bounded_panel("mt", partial=True)

    def test_single_native_action_requires_explicit_capacity_before_claim(self) -> None:
        step = self.start_build()
        with self.assertRaises(review.WorkflowError) as refused:
            transport.run_build_batch(step, mock.Mock())
        self.assertEqual(refused.exception.code, "native_capacity_required")
        self.assertEqual(self.build_state()["bl_workflow"]["action"]["status"], "ready")

    def test_loop_review_items_omit_local_next_and_name_the_owner(self) -> None:
        for loop, verb in (("bl", "build-next"), ("mt", "measure-twice-next")):
            with self.subTest(loop=loop):
                self.session = "codex-next-" + uuid.uuid4().hex
                step = self.six_seat_panel(loop)
                item = step.work_items[0]
                self.assertNotIn("next", item.review_item.commands)
                claim = review.claim_review_action(
                    review.ClaimRequest(item.review_ref, item.action_id)
                )
                self.assertNotIn("next", claim.work_item.commands)
                with self.assertRaises(review.WorkflowError) as refused:
                    review.next_review(item.review_ref)
                self.assertIn(verb, str(refused.exception))
        standalone = self.start_review("sol")
        self.assertEqual(standalone.work_items[0].commands["next"][1], "review-next")

    def test_native_default_effort_matches_external_and_is_frozen(self) -> None:
        step = self.start_review("sol")
        item = step.work_items[0]
        self.assertEqual(item.native_transport["spawn"]["reasoning_effort"], "xhigh")
        record = review.review_runs.read_run_json(Path(item.prompt_path).parents[3])
        self.assertEqual(record["seat_signatures"]["sol"]["reasoning_effort"], "xhigh")

    def test_changed_prompt_cannot_settle_native_writer(self) -> None:
        step = self.start_build()
        item = self.claim_writer(step)
        Path(item.prompt_path).write_text("different prompt")
        with self.assertRaises(review.WorkflowError) as refused:
            build.capture_build_return(
                step.ref,
                item.action_id,
                COMPLETED,
                handle="writer",
                completion_observed=True,
            )
        self.assertEqual(refused.exception.code, "invalid_native_binding")
        self.assertEqual(
            self.build_state()["bl_workflow"]["outstanding_writer"], item.action_id
        )

    def test_cli_binding_uses_frozen_channel_and_refuses_artifact_capture(self) -> None:
        step = self.start_review("sol")
        item = step.work_items[0]
        review.claim_review_action(review.ClaimRequest(step.ref, item.action_id))
        with mock.patch.dict(os.environ, {"CREW_HOST": "claude"}):
            code, result = self.cli_call(
                [*item.commands["native_bind"][1:], "--handle", "frozen-reviewer"]
            )
            self.assertEqual((code, result["code"]), (2, "host_mismatch"))
        code, result = self.cli_call(
            [*item.commands["native_bind"][1:], "--handle", "frozen-reviewer"]
        )
        self.assertEqual((code, result["bound"]), (0, True))
        argv = [
            *item.commands["native_bind"][1:],
            "--handle",
            "frozen-reviewer",
            "--output-file",
            "/unused",
        ]
        argv[0] = "review-native-capture"
        code, result = self.cli_call(argv)
        self.assertEqual((code, result["code"]), (2, "unsupported_native_capture"))
        writer = self.start_build()
        build.claim_build_action(writer.ref, writer.work_items[0].action_id)
        self.assertTrue(
            build.bind_build_native(
                writer.ref, writer.work_items[0].action_id, "frozen-writer"
            )["bound"]
        )
        with self.assertRaises(review.WorkflowError) as refused:
            build.capture_build_native(
                writer.ref,
                writer.work_items[0].action_id,
                "frozen-writer",
                "/unused",
                completion_observed=True,
            )
        self.assertEqual(refused.exception.code, "unsupported_native_capture")

    def test_codex_capture_flags_are_rejected_for_other_channels(self) -> None:
        with self.assertRaises(review.WorkflowError) as refused:
            transport.require_capture_flags("claude", None, False, True)
        self.assertEqual(refused.exception.code, "unsupported_native_capture")

    def test_launch_refusal_rechecks_binding_when_workspace_observation_races(
        self,
    ) -> None:
        step = self.start_build()
        item = step.work_items[0]
        build.claim_build_action(step.ref, item.action_id)
        observe = execution.observe_workspace
        bound = False

        def bind_during_observation(path: str) -> execution.WorkspaceFacts:
            nonlocal bound
            if not bound:
                bound = True
                build.bind_build_native(step.ref, item.action_id, "concurrent-writer")
            return observe(path)

        with (
            mock.patch.object(
                execution, "observe_workspace", side_effect=bind_during_observation
            ),
            self.assertRaises(review.WorkflowError) as refused,
        ):
            build.capture_build_return(
                step.ref,
                item.action_id,
                b"",
                status="failed",
                diagnostic="launch refused",
                launch_refused=True,
            )
        self.assertEqual(refused.exception.code, "invalid_native_binding")
        state = self.build_state()["bl_workflow"]
        self.assertEqual(state["outstanding_writer"], item.action_id)
        self.assertEqual(state["action"]["handle"], "concurrent-writer")
        self.assertEqual(state["accepted_actions"], [])

    def test_public_cli_bind_and_host_written_capture_requires_owned_handle(
        self,
    ) -> None:
        step = self.start_build()
        item = step.work_items[0]
        self.cli_call(list(item.commands["claim"])[1:])
        code, bound = self.cli_call(
            [*item.commands["native_bind"][1:], "--handle", "cli-writer"]
        )
        self.assertEqual((code, bound["handle"]), (0, "cli-writer"))
        Path(item.returned_path).parent.mkdir(parents=True, exist_ok=True)
        Path(item.returned_path).write_bytes(COMPLETED)
        code, refused = self.cli_call(list(item.commands["capture"])[1:])
        self.assertEqual((code, refused["code"]), (2, "completion_not_observed"))
        code, accepted = self.cli_call(
            [
                *item.commands["capture"][1:],
                "--handle",
                "cli-writer",
                "--completion-observed",
            ]
        )
        self.assertEqual((code, accepted["type"]), (0, "work_batch"))
        self.assertTrue(
            any(
                path.read_bytes() == COMPLETED
                for path in (self.root / ".crew").glob("**/returns/*.txt")
            )
        )

    def test_public_planning_cli_uses_generated_argv_and_bound_capture(self) -> None:
        step = self.start_measure()
        encoded = measure.step_to_dict(step)["work_items"][0]
        commands = encoded["commands_argv"]
        self.assertEqual(commands["next"][1], "measure-twice-next")
        self.assertNotIn("native_capture", commands)
        code, claimed = self.cli_call(commands["claim"][1:])
        self.assertEqual((code, claimed["authorization"]), (0, "spawn"))
        code, bound = self.cli_call(
            [*commands["native_bind"][1:], "--handle", "cli-advisor"]
        )
        self.assertEqual((code, bound["handle"]), (0, "cli-advisor"))
        Path(encoded["staging_path"]).write_bytes(PLAN)
        returned = Path(encoded["native_transport"]["returned_path"])
        returned.parent.mkdir(parents=True, exist_ok=True)
        returned.write_bytes(b"Plan written.")
        code, captured = self.cli_call(
            [
                *commands["capture"][1:],
                "--returned-file",
                str(returned),
                "--handle",
                "cli-advisor",
                "--completion-observed",
            ]
        )
        self.assertEqual((code, captured["type"]), (0, "work_batch"))
        self.assertTrue(
            all(
                item["commands_argv"]["next"][1] == "measure-twice-next"
                for item in captured["work_items"]
            )
        )
        self.assertEqual(Path(self.measure_state()["plan_file"]).read_bytes(), PLAN)

    def test_public_review_cli_records_refusal_without_substitution(self) -> None:
        step = self.start_review("sol")
        item = step.work_items[0]
        self.cli_call(item.commands["claim"][1:])
        returned = Path(item.native_transport["returned_path"])
        returned.write_bytes(b"Unsupported exact requested model.")
        code, failed = self.cli_call(
            [
                *item.commands["capture"][1:],
                "--returned-file",
                str(returned),
                "--status",
                "failed",
                "--diagnostic",
                "Unsupported exact requested model.",
                "--launch-refused",
            ]
        )
        self.assertEqual((code, failed["outcome"]["status"]), (0, "all_failed"))


if __name__ == "__main__":
    unittest.main()
