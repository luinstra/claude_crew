"""Native OpenHands fixtures: persisted events, no provider or Canvas calls."""

from __future__ import annotations

import dataclasses
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
from multiagent import build_workflow as build
from multiagent import config, execution, seats
from multiagent import measure_twice as measure
from multiagent import openhands_native_transport as native
from multiagent import review_workflow as review
from multiagent import workflow_transport as transport

VALID = "## VERDICT\nAPPROVED\n\n## FINDINGS\nnone\n"


class OpenHandsTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.conv = self.root / "host-conversation"
        (self.conv / "events").mkdir(parents=True)
        self.state = {
            "id": str(uuid.uuid4()),
            "workspace": {"working_dir": str(self.root)},
            "agent": {
                "kind": "Agent",
                "llm": {"model": "configured-model"},
                "tools": [
                    {"name": name}
                    for name in ["terminal", "file_editor", "task_tool_set"]
                ],
            },
        }
        self.write_state()
        (self.root / "home").mkdir()
        self.env = mock.patch.dict(
            os.environ,
            {
                "HOME": str(self.root / "home"),
                "CREW_PROJECT_DIR": str(self.root),
                "CLAUDE_PROJECT_DIR": "",
                "CREW_HOST": "openhands",
                "CREW_OPENHANDS_CONTEXT": str(self.root / "context.json"),
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        home = mock.patch("pathlib.Path.home", return_value=self.root / "home")
        home.start()
        self.addCleanup(home.stop)
        config._reset_cache_for_tests()
        self.addCleanup(config._reset_cache_for_tests)
        self.host = native.inspect_context("fixture-backend", self.conv, self.root)
        (self.root / "context.json").write_text(
            json.dumps(dataclasses.asdict(self.host))
        )
        self.session = self.host.session_id
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / ".gitignore").write_text(
            ".crew/\nhome/\nhost-conversation/\ncontext.json\n"
        )
        self.plan = self.root / "requirements.md"
        self.plan.write_text("# Plan\nWrite hello.txt and verify its contents.\n")
        self.event_count = 0

    def write_state(self) -> None:
        (self.conv / "base_state.json").write_text(json.dumps(self.state))

    def start_review(self) -> review.ReviewStep:
        return review.start_review(
            review.ReviewRequest(str(self.plan), session_id=self.session)
        )

    def persist_event(self, value: dict) -> Path:
        self.event_count += 1
        path = self.conv / "events" / f"event-{self.event_count:05d}-{value['id']}.json"
        path.write_bytes(json.dumps(value, ensure_ascii=False).encode())
        return path

    def complete(
        self,
        ref: native.Ref,
        item: object,
        text: str = VALID,
        *,
        status: str = "completed",
        denied: bool = False,
        callback: object = None,
    ) -> tuple[object, Path]:
        prepared = native.prepare(ref, item.action_id)
        self.assertEqual(prepared["authorization"], "spawn")
        self.assertEqual(
            native.prepare(ref, item.action_id)["authorization"], "do_not_spawn"
        )
        if callback:
            callback()
        action = {
            "kind": "ActionEvent",
            "id": str(uuid.uuid4()),
            "tool_name": "task",
            "tool_call_id": str(uuid.uuid4()),
            "action": {"kind": "TaskAction", **prepared["task"]},
        }
        self.persist_event(action)
        observation = {
            "kind": "UserRejectObservation" if denied else "ObservationEvent",
            "id": str(uuid.uuid4()),
            "action_id": action["id"],
            "tool_name": "task",
            "tool_call_id": action["tool_call_id"],
        }
        if denied:
            observation["rejection_reason"] = "fixture denied"
        else:
            observation["observation"] = {
                "kind": "TaskObservation",
                "task_id": str(uuid.uuid4()),
                "subagent": prepared["task"]["subagent_type"],
                "status": status,
                "is_error": status == "error",
                "content": [{"type": "text", "text": text}],
            }
        path = self.persist_event(observation)
        return native.capture(ref, item.action_id), path

    def panel(self, step: object, verdict: str = "APPROVED") -> object:
        for item in step.work_items:
            self.complete(item.review_ref, item.review_item)
        advance = (
            build.next_build
            if isinstance(step, build.BuildStep)
            else measure.next_measure_twice
        )
        synthesis = advance(step.ref).work_items[0]
        review.claim_review_action(
            review.ClaimRequest(synthesis.review_ref, synthesis.action_id)
        )
        transport.capture_review_return(
            synthesis.review_ref,
            synthesis.action_id,
            f"Full synthesis\nBLOCKING_CAUSES: {1 if verdict == 'REVISE' else 0}\n".encode(),
            judgment=transport.SynthesisJudgment(verdict),
        )
        return advance(step.ref)

    def test_default_ignores_external_global_configuration(self) -> None:
        (self.root / "home/.crew-config.toml").write_text(
            'default_panel = "full"\n[build]\nexecutor = "sol"\n'
        )
        config._reset_cache_for_tests()
        step = self.start_review()
        self.assertEqual(len(step.work_items), 1)
        item = step.work_items[0]
        self.assertEqual(
            (item.channel, item.model, item.role),
            ("openhands", "inherit", "crew:reviewer"),
        )
        self.assertEqual(
            item.native_transport["task"]["subagent_type"],
            self.host.routes["inherit"]["roles"]["crew:reviewer"],
        )
        self.assertEqual(
            execution.resolve_executor(self.session).executor, "crew:executor"
        )

    def test_unknown_external_duplicate_profiles_and_effort_fail(self) -> None:
        for roster in ["sol", "no-such-profile", "openhands-inherit,openhands-inherit"]:
            with self.subTest(roster=roster), self.assertRaises(review.WorkflowError):
                review.start_review(
                    review.ReviewRequest(
                        str(self.plan), seats=roster, session_id=self.session
                    )
                )

    def test_exact_text_replay_and_changed_event_rejection(self) -> None:
        step = self.start_review()
        item = step.work_items[0]
        raw = VALID.replace("\n", "\r\n") + "\n```text\n  café 雪\n```\n"
        result, path = self.complete(step.ref, item, raw)
        self.assertEqual(result.work_items[0].kind, "formatter")
        actual, proof = native.extract(step.ref, item.action_id)
        self.assertEqual(actual, raw.encode())
        self.assertEqual(proof["returned_sha256"], measure.sha256(raw.encode()))
        native.capture(step.ref, item.action_id)
        event = json.loads(path.read_bytes())
        event["observation"]["content"][0]["text"] += "changed"
        path.write_text(json.dumps(event))
        with self.assertRaises(review.WorkflowError):
            native.capture(step.ref, item.action_id)

    def test_no_model_retyped_capture_or_uncertain_relaunch(self) -> None:
        step = self.start_review()
        item = step.work_items[0]
        native.prepare(step.ref, item.action_id)
        with self.assertRaises(review.WorkflowError):
            transport.capture_review_return(step.ref, item.action_id, VALID.encode())
        with self.assertRaises(review.WorkflowError):
            native.capture(step.ref, item.action_id)
        self.assertEqual(
            native.prepare(step.ref, item.action_id)["authorization"], "do_not_spawn"
        )

    def test_permission_denial_is_failed_owned_result(self) -> None:
        step = self.start_review()
        result, _ = self.complete(step.ref, step.work_items[0], denied=True)
        self.assertEqual(result.type, "terminal")
        self.assertEqual(result.outcome["status"], "all_failed")

    def test_acp_missing_tools_and_changed_profile_fail_closed(self) -> None:
        for alteration in [
            {"kind": "ACPAgent"},
            {"tools": []},
            {"llm": {"model": "changed"}},
        ]:
            original = self.state["agent"].copy()
            self.state["agent"].update(alteration)
            self.write_state()
            with self.assertRaises(review.WorkflowError):
                self.start_review()
            self.state["agent"] = original
            self.write_state()

    def test_debate_finishes_without_judgment_or_formatter(self) -> None:
        step = review.start_debate(
            review.DebateRequest("Which approach?", session_id=self.session)
        )
        self.assertEqual(step.work_items[0].role, "crew:panelist")
        result, _ = self.complete(
            step.ref, step.work_items[0], "An independent position."
        )
        item = result.work_items[0]
        self.assertEqual(item.kind, "synthesis")
        review.claim_review_action(review.ClaimRequest(step.ref, item.action_id))
        result = transport.capture_review_return(
            step.ref, item.action_id, b"Consolidated positions.\n"
        )
        self.assertEqual(result.type, "terminal")
        self.assertIsNone(result.outcome["judgment"])

    def test_measure_revision_and_terminal_replay(self) -> None:
        step = measure.start_measure_twice(
            measure.MeasureRequest("requirements.md", self.session)
        )
        item = step.work_items[0]
        reviewed, _ = self.complete(
            step.ref,
            item,
            "Plan written.",
            callback=lambda item=item: Path(item.staging_path).write_text(
                "# Plan\nWrite hello.txt; verify exact bytes.\n"
            ),
        )
        revised = self.panel(reviewed, "REVISE")
        second = revised.work_items[0]
        self.assertNotEqual(item.action_id, second.action_id)
        reviewed, _ = self.complete(
            revised.ref,
            second,
            "Revised plan written.",
            callback=lambda: Path(second.staging_path).write_text(
                "# Revised plan\nWrite hello.txt; assert hello newline.\n"
            ),
        )
        done = self.panel(reviewed)
        self.assertEqual(done.outcome["status"], "approved")
        self.assertEqual(measure.next_measure_twice(step.ref), done)
        self.assertFalse((self.root / "hello.txt").exists())

    def test_build_revision_fresh_writer_and_terminal_replay(self) -> None:
        step = build.start_build(build.BuildRequest("Write hello.txt", self.session))
        item = step.work_items[0]
        reviewed, _ = self.complete(
            step.ref,
            item,
            "Implemented\nCREW_BUILD_STATUS: COMPLETED",
            callback=lambda: (self.root / "hello.txt").write_text("hello\n"),
        )
        self.assertEqual((self.root / "hello.txt").read_bytes(), b"hello\n")
        revised = self.panel(reviewed, "REVISE")
        second = revised.work_items[0]
        self.assertNotEqual(item.action_id, second.action_id)
        self.assertNotIn("resume", second.native_transport["task"])
        reviewed, _ = self.complete(
            revised.ref, second, "Verified\nCREW_BUILD_STATUS: COMPLETED"
        )
        done = self.panel(reviewed)
        self.assertEqual(done.outcome["status"], "approved")
        self.assertEqual(build.resume_build(self.session), done)

    def test_failed_build_marker_and_cancelled_advisor(self) -> None:
        step = build.start_build(build.BuildRequest("Write hello.txt", self.session))
        failed, _ = self.complete(
            step.ref,
            step.work_items[0],
            "Verification failed\nCREW_BUILD_STATUS: BLOCKED",
        )
        self.assertEqual(failed.type, "needs_input")
        build.cancel_build(step.ref, "fixture")

    def test_quiescence_recovery_releases_intent_and_rejects_late_result(self) -> None:
        step = self.start_review()
        item = step.work_items[0]
        native.prepare(step.ref, item.action_id)
        with self.assertRaises(review.WorkflowError):
            native.recover(step.ref, item.action_id, "unknown")
        result = native.recover(step.ref, item.action_id, "not_running")
        self.assertEqual(result.outcome["status"], "all_failed")
        with self.assertRaises(review.WorkflowError):
            native.capture(step.ref, item.action_id)

    def test_wrong_workspace_and_explicit_external_executor_refused(self) -> None:
        with self.assertRaises(review.WorkflowError):
            native.inspect_context("backend", self.conv, self.root / "other")
        with self.assertRaises((review.WorkflowError, execution.ExecutionError)):
            build.start_build(
                build.BuildRequest("--executor sol Write hello.txt", self.session)
            )

    def test_wrapper_preserves_target_separator_and_supplies_native_session(
        self,
    ) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(native.PLUGIN / "scripts/openhands.py"),
                "run",
                "--backend-id",
                "fixture-backend",
                "--conversation-dir",
                str(self.conv),
                "--workspace",
                str(self.root),
                "--",
                "review",
                "--",
                str(self.plan),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["ref"]["session_segment"], self.session)
        self.assertEqual(value["work_items"][0]["channel"], "openhands")

    def owner_fixture(self, kind: str) -> tuple[object, object, object]:
        task = f"requirements-{uuid.uuid4().hex}.md"
        (self.root / task).write_text("# Plan\nWrite hello and check.\n")
        if kind == "review":
            step = review.start_review(
                review.ReviewRequest(str(self.root / task), session_id=self.session)
            )
            return step.ref, step.work_items[0], None
        if kind == "measure":
            step = measure.start_measure_twice(
                measure.MeasureRequest(task, self.session)
            )
            return (
                step.ref,
                step.work_items[0],
                lambda: measure.cancel_measure_twice(step.ref, "fixture"),
            )
        step = build.start_build(
            build.BuildRequest(f"Write hello.txt per {task}", self.session)
        )
        return (
            step.ref,
            step.work_items[0],
            lambda: build.cancel_build(step.ref, "fixture"),
        )

    def test_reservation_claim_crashes_remain_fenced_and_recoverable(self) -> None:
        for kind in ("review", "measure", "build"):
            for after_claim in (False, True):
                with self.subTest(kind=kind, after_claim=after_claim):
                    ref, item, cancel = self.owner_fixture(kind)
                    module, name = {
                        "review": (review, "claim_review_action"),
                        "measure": (measure, "claim_measure_action"),
                        "build": (build, "claim_build_action"),
                    }[kind]
                    original = getattr(module, name)

                    def interrupt(
                        *args: object,
                        after_claim: bool = after_claim,
                        original: object = original,
                        **kwargs: object,
                    ) -> None:
                        if after_claim:
                            original(*args, **kwargs)
                        raise OSError("injected claim boundary")

                    with (
                        mock.patch.object(module, name, side_effect=interrupt),
                        self.assertRaises(OSError),
                    ):
                        native.prepare(ref, item.action_id)
                    self.assertTrue(native._busy(self.session))
                    self.assertEqual(
                        native.prepare(ref, item.action_id)["authorization"],
                        "do_not_spawn",
                    )
                    native.recover(ref, item.action_id, "not_running")
                    self.assertFalse(native._busy(self.session))
                    if cancel:
                        cancel()

    def test_retirement_write_crash_reconciles_every_owner(self) -> None:
        for kind in ("review", "measure", "build"):
            with self.subTest(kind=kind):
                ref, item, cancel = self.owner_fixture(kind)
                native.prepare(ref, item.action_id)
                with (
                    mock.patch.object(
                        native, "_retire", side_effect=OSError("injected retirement")
                    ),
                    self.assertRaises(OSError),
                ):
                    native.recover(ref, item.action_id, "not_running")
                self.assertTrue(native._busy(self.session))
                result = native.recover(ref, item.action_id, "not_running")
                self.assertEqual(result["status"], "retired")
                self.assertEqual(
                    native.recover(ref, item.action_id, "not_running"), result
                )
                self.assertFalse(native._busy(self.session))
                if cancel:
                    cancel()

    def test_existing_claim_without_intent_never_authorizes_spawn(self) -> None:
        for kind in ("review", "measure", "build"):
            with self.subTest(kind=kind):
                ref, item, cancel = self.owner_fixture(kind)
                if kind == "review":
                    review.claim_review_action(review.ClaimRequest(ref, item.action_id))
                elif kind == "measure":
                    measure.claim_measure_action(ref, item.action_id)
                else:
                    build.claim_build_action(ref, item.action_id)
                self.assertEqual(
                    native.prepare(ref, item.action_id)["authorization"], "do_not_spawn"
                )
                native.recover(ref, item.action_id, "not_running")
                if cancel:
                    cancel()

    def test_cancelled_advisor_and_loop_reviewer_retire_without_advancing(self) -> None:
        ref, item, cancel = self.owner_fixture("measure")
        native.prepare(ref, item.action_id)
        cancel()
        before = measure.next_measure_twice(ref)
        native.recover(ref, item.action_id, "not_running")
        self.assertEqual(measure.next_measure_twice(ref), before)
        step = measure.start_measure_twice(
            measure.MeasureRequest("requirements.md", self.session)
        )
        advisor = step.work_items[0]
        reviewed, _ = self.complete(
            step.ref,
            advisor,
            "Plan written",
            callback=lambda: Path(advisor.staging_path).write_text(
                "# Plan\nCheck hello.\n"
            ),
        )
        reviewer = reviewed.work_items[0]
        native.prepare(reviewer.review_ref, reviewer.action_id)
        measure.cancel_measure_twice(step.ref, "fixture")
        before = measure.next_measure_twice(step.ref)
        native.recover(reviewer.review_ref, reviewer.action_id, "not_running")
        self.assertEqual(measure.next_measure_twice(step.ref), before)
        self.assertFalse(native._busy(self.session))
        self.start_review()

    def test_source_receipt_alone_does_not_release_fence(self) -> None:
        step = self.start_review()
        item = step.work_items[0]
        with (
            mock.patch.object(
                transport, "capture_review_return", side_effect=OSError("after source")
            ),
            self.assertRaises(OSError),
        ):
            self.complete(step.ref, item)
        self.assertTrue(native._path(step.ref, item.action_id, "source").exists())
        self.assertTrue(native._busy(self.session))
        second = review.start_debate(
            review.DebateRequest("Other work?", session_id=self.session)
        )
        with self.assertRaisesRegex(review.WorkflowError, "outstanding"):
            native.prepare(second.ref, second.work_items[0].action_id)
        native.capture(step.ref, item.action_id)
        self.assertFalse(native._busy(self.session))

    def test_capture_retirement_crash_replays_after_owner_settlement(self) -> None:
        step = self.start_review()
        with (
            mock.patch.object(native, "_retire", side_effect=OSError("after capture")),
            self.assertRaises(OSError),
        ):
            self.complete(step.ref, step.work_items[0])
        self.assertTrue(native._busy(self.session))
        self.assertEqual(
            native.capture(step.ref, step.work_items[0].action_id)["status"], "retired"
        )
        self.assertFalse(native._busy(self.session))

    def test_settled_cancelled_owner_retirement_keeps_capture_reason(self) -> None:
        for kind in ("build", "measure"):
            with self.subTest(kind=kind):
                ref, item, cancel = self.owner_fixture(kind)
                with (
                    mock.patch.object(
                        native, "_retire", side_effect=OSError("after settlement")
                    ),
                    self.assertRaises(OSError),
                ):
                    self.complete(ref, item, "failed task", status="error")
                cancel()
                self.assertEqual(
                    native.capture(ref, item.action_id)["reason"], "captured"
                )
                self.assertFalse(native._busy(self.session))

    def test_hooks_are_ignored_and_scaffold_errors_remain_outstanding(self) -> None:
        step = self.start_review()
        item = step.work_items[0]
        with mock.patch.object(native, "capture", return_value=None):
            _, path = self.complete(step.ref, item, VALID + "café\r\n")
        observation = json.loads(path.read_bytes())
        self.persist_event(
            {
                "kind": "HookExecutionEvent",
                "id": str(uuid.uuid4()),
                "action_id": observation["action_id"],
                "hook_event": "PostToolUse",
            }
        )
        self.assertEqual(
            native.extract(step.ref, item.action_id)[0], (VALID + "café\r\n").encode()
        )
        native.capture(step.ref, item.action_id)
        other = review.start_debate(
            review.DebateRequest("Error probe", session_id=self.session)
        )
        with mock.patch.object(native, "capture", return_value=None):
            _, path = self.complete(other.ref, other.work_items[0])
        observation = json.loads(path.read_bytes())
        error = {key: observation[key] for key in ("id", "tool_name", "tool_call_id")}
        error.update(kind="AgentErrorEvent", error="Tool crashed café\r\n")
        path.write_text(json.dumps(error))
        with self.assertRaises(review.WorkflowError) as error:
            native.capture(other.ref, other.work_items[0].action_id)
        self.assertEqual(error.exception.code, "completion_not_observed")
        self.assertTrue(native._busy(self.session))
        native.recover(other.ref, other.work_items[0].action_id, "not_running")

    def test_same_model_configuration_changes_require_new_context(self) -> None:
        native.select_context(self.host)
        step = self.start_review()
        item = step.work_items[0]
        native.prepare(step.ref, item.action_id)
        self.state["agent"]["llm"]["reasoning_effort"] = "high"
        self.state["agent"]["llm"]["base_url"] = "https://other-provider.invalid/v1"
        self.state["agent"]["llm"]["api_key"] = "never-persist-me"
        self.state["agent"]["tools"][2]["params"] = {"max_tasks": 3}
        self.write_state()
        changed = native.inspect_context("fixture-backend", self.conv, self.root)
        self.assertNotEqual(self.host.effective_sha256, changed.effective_sha256)
        with self.assertRaises(review.WorkflowError):
            native.select_context(changed)
        with self.assertRaises(review.WorkflowError):
            native.select_context(changed, new_generation=True)
        with self.assertRaises(review.WorkflowError):
            native.prepare(step.ref, item.action_id)
        native.recover(step.ref, item.action_id, "not_running")
        snapshot = native.select_context(changed, new_generation=True)
        self.assertNotIn("never-persist-me", snapshot.read_text())
        self.assertNotIn("other-provider", snapshot.read_text())
        os.environ["CREW_OPENHANDS_CONTEXT"] = str(snapshot)
        self.start_review()
        rendered = native.launch_metadata(
            step.ref,
            item.action_id,
            item.role,
            item.model,
            None,
            item.prompt_path,
            "unused",
        )
        self.assertEqual(rendered["task"], item.native_transport["task"])
        with self.assertRaises(review.WorkflowError):
            native.prepare(step.ref, item.action_id)

    def test_each_effective_setting_is_fingerprinted(self) -> None:
        for target, key, value in [
            (self.state["agent"]["llm"], "reasoning_effort", "high"),
            (self.state["agent"]["llm"], "base_url", "https://route.invalid"),
            (self.state["agent"]["llm"], "max_output_tokens", 10),
            (self.state["agent"]["tools"][2], "params", {"limit": 2}),
        ]:
            target[key] = value
            self.write_state()
            self.assertNotEqual(
                self.host.effective_sha256,
                native.inspect_context(
                    "fixture-backend", self.conv, self.root
                ).effective_sha256,
            )
            del target[key]

    def test_capture_flags_are_codex_only(self) -> None:
        for args in (
            ("handle", False, False),
            (None, True, False),
            (None, False, True),
        ):
            with self.assertRaises(review.WorkflowError):
                transport.require_capture_flags("openhands", *args)

    def test_effort_override_is_refused(self) -> None:
        spec = seats.SeatSpec(
            "effort", ("openhands",), model="inherit", reasoning_effort="high"
        )
        with (
            mock.patch.object(seats, "seat_spec", return_value=spec),
            self.assertRaisesRegex(review.WorkflowError, "reasoning"),
        ):
            review.start_review(
                review.ReviewRequest(
                    str(self.plan), seats="effort", session_id=self.session
                )
            )

    def test_named_profile_review_end_to_end(self) -> None:
        exported = self.root / "exported"
        profiles = self.root / "profiles"
        profiles.mkdir()
        (profiles / "configured-a.json").write_text(
            json.dumps({"model": "profile-model", "api_key": "never-copy"})
        )
        subprocess.run(
            [
                sys.executable,
                str(native.PLUGIN / "scripts/openhands-package.py"),
                "--output",
                str(exported),
                "--profile",
                "configured-a",
                "--profile-store-dir",
                str(profiles),
            ],
            check=True,
        )
        with mock.patch.object(native, "PLUGIN", exported):
            host = native.inspect_context("fixture-backend", self.conv, self.root)
            (self.root / "context.json").write_text(
                json.dumps(dataclasses.asdict(host))
            )
            spec = seats.SeatSpec("profile-a", ("openhands",), model="configured-a")
            with mock.patch.object(seats, "seat_spec", return_value=spec):
                step = review.start_review(
                    review.ReviewRequest(
                        str(self.plan), seats="profile-a", session_id=self.session
                    )
                )
                result, _ = self.complete(step.ref, step.work_items[0])
                self.assertEqual(result.work_items[0].kind, "synthesis")
            self.assertEqual(
                step.work_items[0].native_transport["profile"], "configured-a"
            )
            (profiles / "configured-a.json").write_text(
                json.dumps({"model": "profile-model", "reasoning_effort": "high"})
            )
            with self.assertRaises(review.WorkflowError):
                native.context()

    def test_capture_write_boundaries_for_all_owners(self) -> None:
        for kind in ("review", "measure", "build"):
            for boundary in ("source", "retirement"):
                with self.subTest(kind=kind, boundary=boundary):
                    ref, item, cancel = self.owner_fixture(kind)
                    callback = (
                        (
                            lambda item=item: Path(item.staging_path).write_text(
                                "# Plan\nVerify hello.\n"
                            )
                        )
                        if kind == "measure"
                        else None
                    )
                    raw = (
                        VALID
                        if kind == "review"
                        else "Done\nCREW_BUILD_STATUS: COMPLETED"
                    )
                    module, name = (
                        (native, "_retire")
                        if boundary == "retirement"
                        else (build, "capture_build_return")
                        if kind == "build"
                        else (transport, "capture_measure_return")
                        if kind == "measure"
                        else (transport, "capture_review_return")
                    )
                    with (
                        mock.patch.object(
                            module, name, side_effect=OSError("write boundary")
                        ),
                        self.assertRaises(OSError),
                    ):
                        self.complete(ref, item, raw, callback=callback)
                    self.assertTrue(native._busy(self.session))
                    native.capture(ref, item.action_id)
                    self.assertFalse(native._busy(self.session))
                    if cancel:
                        cancel()

    def test_quiescence_receipt_write_failure_and_drift_recovery(self) -> None:
        for kind in ("review", "measure", "build"):
            with self.subTest(kind=kind):
                ref, item, cancel = self.owner_fixture(kind)
                native.prepare(ref, item.action_id)
                with (
                    mock.patch.object(
                        measure,
                        "_write_once",
                        side_effect=OSError("quiescence receipt"),
                    ),
                    self.assertRaises(OSError),
                ):
                    native.recover(ref, item.action_id, "not_running")
                self.assertEqual(
                    native._owner_status(ref, item.action_id)[1], "claimed"
                )
                self.state["agent"]["llm"]["reasoning_effort"] = "high"
                self.write_state()
                native.recover(ref, item.action_id, "not_running")
                self.assertFalse(native._busy(self.session))
                del self.state["agent"]["llm"]["reasoning_effort"]
                self.write_state()
                if cancel:
                    cancel()

    def test_cancelled_advisor_completion_and_replacement_retirement(self) -> None:
        ref, item, cancel = self.owner_fixture("measure")
        with mock.patch.object(native, "capture", return_value=None):
            self.complete(ref, item, "Cancelled task completed", callback=cancel)
        before = measure.next_measure_twice(ref)
        self.assertEqual(native.capture(ref, item.action_id)["status"], "retired")
        self.assertEqual(measure.next_measure_twice(ref), before)
        ref, item, cancel = self.owner_fixture("measure")
        native.prepare(ref, item.action_id)
        cancel()
        replacement, _, cancel_replacement = self.owner_fixture("measure")
        native.recover(ref, item.action_id, "not_running")
        self.assertFalse(native._busy(self.session))
        self.assertEqual(measure.next_measure_twice(replacement).type, "work_batch")
        cancel_replacement()

    def test_missing_backend_event_store_fails_before_work(self) -> None:
        (self.conv / "events").rmdir()
        with self.assertRaisesRegex(review.WorkflowError, "event directory"):
            self.start_review()
        self.assertFalse((self.root / ".crew/openhands/transport").exists())

    def test_frozen_prompt_reads_preserve_crlf(self) -> None:
        path = self.root / "prompt.txt"
        raw = "one\r\ntwo\r\n"
        path.write_bytes(raw.encode())
        review._read_authoritative_prompt(path, raw, "fixture")
        with self.assertRaises(review.WorkflowError):
            review._read_authoritative_prompt(
                path, raw.replace("\r\n", "\n"), "fixture"
            )

    def test_interruption_error_keeps_active_and_cancelled_writer_fences(self) -> None:
        for cancelled in (False, True):
            for resolution in ("completion", "confirmed_recovery"):
                with self.subTest(cancelled=cancelled, resolution=resolution):
                    ref, item, cancel = self.owner_fixture("build")
                    with mock.patch.object(native, "capture", return_value=None):
                        _, path = self.complete(
                            ref, item, "Done\nCREW_BUILD_STATUS: COMPLETED"
                        )
                    observation = json.loads(path.read_bytes())
                    path.write_text(
                        json.dumps(
                            {
                                "kind": "AgentErrorEvent",
                                "id": str(uuid.uuid4()),
                                "tool_name": "task",
                                "tool_call_id": observation["tool_call_id"],
                                "error": "Tool call interrupted before completion. The conversation was paused.",
                            }
                        )
                    )
                    if cancelled:
                        cancel()
                    with self.assertRaises(review.WorkflowError) as error:
                        native.capture(ref, item.action_id)
                    self.assertEqual(error.exception.code, "completion_not_observed")
                    self.assertTrue(native._busy(self.session))
                    self.assertEqual(
                        build._transaction(
                            ref, lambda _data, journal: journal.outstanding_writer
                        ),
                        item.action_id,
                    )
                    if resolution == "completion":
                        self.persist_event(observation)
                        native.capture(ref, item.action_id)
                    else:
                        native.recover(ref, item.action_id, "not_running")
                        self.persist_event(observation)
                        with self.assertRaises(review.WorkflowError):
                            native.capture(ref, item.action_id)
                    self.assertFalse(native._busy(self.session))
                    self.assertIsNone(
                        build._transaction(
                            ref, lambda _data, journal: journal.outstanding_writer
                        )
                    )
                    cancel()

    def named_package(self) -> tuple[Path, Path, Path]:
        exported = self.root / "named-package"
        profiles = self.root / "profiles"
        profiles.mkdir()
        profile = profiles / "configured-a.json"
        profile.write_text(
            json.dumps(
                {"model": "profile-model", "provider_connection_id": "connection-a"}
            )
        )
        connections = self.root / "provider-connections/provider_connections.json"
        connections.parent.mkdir()
        connections.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "connections": [
                        {
                            "id": "connection-a",
                            "display_name": "Fixture",
                            "provider": "custom",
                            "base_url": "https://original.invalid/v1",
                            "api_key": "secret-before",
                            "created_at": 1,
                            "updated_at": 1,
                        }
                    ],
                }
            )
        )
        subprocess.run(
            [
                sys.executable,
                str(native.PLUGIN / "scripts/openhands-package.py"),
                "--output",
                str(exported),
                "--profile",
                "configured-a",
                "--profile-store-dir",
                str(profiles),
            ],
            check=True,
        )
        return exported, profile, connections

    def test_provider_connection_routing_drift_recovery_and_new_context(self) -> None:
        exported, profile, connections = self.named_package()
        with mock.patch.object(native, "PLUGIN", exported):
            host = native.inspect_context("fixture-backend", self.conv, self.root)
            snapshot = native.select_context(host)
            os.environ["CREW_OPENHANDS_CONTEXT"] = str(snapshot)
            spec = seats.SeatSpec("profile-a", ("openhands",), model="configured-a")
            with mock.patch.object(seats, "seat_spec", return_value=spec):
                step = review.start_review(
                    review.ReviewRequest(
                        str(self.plan),
                        seats="profile-a,openhands-inherit",
                        session_id=self.session,
                    )
                )
            item = step.work_items[0]
            native.prepare(step.ref, item.action_id)
            original_profile = profile.read_bytes()
            value = json.loads(connections.read_bytes())
            value["connections"][0]["base_url"] = "https://changed.invalid/v1"
            connections.write_text(json.dumps(value))
            self.assertEqual(profile.read_bytes(), original_profile)
            with self.assertRaises(review.WorkflowError) as error:
                native.context()
            self.assertEqual(error.exception.code, "host_configuration_changed")
            changed = native.inspect_context("fixture-backend", self.conv, self.root)
            self.assertNotEqual(host.profile_hashes, changed.profile_hashes)
            with self.assertRaises(review.WorkflowError):
                native.select_context(changed, new_generation=True)
            result = native.recover(step.ref, item.action_id, "not_running")
            self.assertEqual(result.type, "work_batch")
            self.assertEqual(
                native._owner_status(step.ref, item.action_id)[1], "settled"
            )
            self.assertFalse(native._busy(self.session))
            snapshot = native.select_context(changed, new_generation=True)
            for secret in ("original.invalid", "changed.invalid", "secret-before"):
                self.assertNotIn(secret, snapshot.read_text())
            os.environ["CREW_OPENHANDS_CONTEXT"] = str(snapshot)
            self.assertEqual(native.context(), changed)
            with self.assertRaises(review.WorkflowError):
                native.prepare(step.ref, result.work_items[0].action_id)

    def test_supported_credentials_rotate_without_routing_drift(self) -> None:
        exported, profile, connections = self.named_package()
        secret_fields = (
            "api_key",
            "aws_access_key_id",
            "aws_secret_access_key",
            "aws_session_token",
        )
        with mock.patch.object(native, "PLUGIN", exported):
            for field in secret_fields:
                self.state["agent"]["llm"][field] = "gAAAA-before"
            self.write_state()
            value = json.loads(profile.read_bytes())
            value.update({field: "fixture-before" for field in secret_fields})
            profile.write_text(json.dumps(value))
            before = native.inspect_context("fixture-backend", self.conv, self.root)
            for field in secret_fields:
                self.state["agent"]["llm"][field] = "gAAAA-after"
                value[field] = "fixture-after"
            self.write_state()
            profile.write_text(json.dumps(value))
            connection = json.loads(connections.read_bytes())
            connection["connections"][0].update(api_key="fixture-after", updated_at=2)
            connections.write_text(json.dumps(connection))
            self.assertEqual(
                before, native.inspect_context("fixture-backend", self.conv, self.root)
            )

    def test_missing_and_malformed_provider_connections_fail_closed(self) -> None:
        exported, _, connections = self.named_package()
        with mock.patch.object(native, "PLUGIN", exported):
            for value in (
                [],
                {"connections": {}},
                {"connections": [None]},
                {"schema_version": "1", "connections": []},
                {"schema_version": 2, "connections": []},
                {"connections": [{"id": "connection-a", "base_url": []}]},
                {"connections": []},
            ):
                with self.subTest(value=value):
                    connections.write_text(json.dumps(value))
                    with self.assertRaises(review.WorkflowError):
                        native.inspect_context("fixture-backend", self.conv, self.root)
            connections.unlink()
            with self.assertRaises(review.WorkflowError):
                native.inspect_context("fixture-backend", self.conv, self.root)

    def test_malformed_host_shapes_raise_typed_errors(self) -> None:
        for key, value in (
            ("workspace", []),
            ("agent", []),
            ("agent", {"kind": "Agent", "llm": []}),
            ("agent", {"kind": "Agent", "tools": {}}),
            ("agent", {"kind": "Agent", "tools": [{}]}),
            ("agent", {"kind": "Agent", "tools": [None]}),
        ):
            with self.subTest(key=key, value=value):
                original = self.state[key]
                self.state[key] = value
                self.write_state()
                with self.assertRaises(review.WorkflowError) as error:
                    native.inspect_context("fixture-backend", self.conv, self.root)
                self.assertEqual(error.exception.code, "invalid_host_evidence")
                self.state[key] = original
        self.write_state()

    def test_corrupt_intents_are_isolated_only_for_known_other_sessions(self) -> None:
        step = self.start_review()
        native.prepare(step.ref, step.work_items[0].action_id)
        intent = native._path(step.ref, step.work_items[0].action_id, "intent")
        intent.write_text("damaged")
        with self.assertRaises(review.WorkflowError):
            native._busy(self.session)
        self.assertFalse(native._busy("another-session"))
        (intent.parent / "context.json").unlink()
        with self.assertRaises(review.WorkflowError):
            native._busy("another-session")

    def run_issued(self, argv: object, *extra: str, success: bool = True) -> object:
        env = dict(os.environ)
        for key in (
            "CREW_HOST",
            "CREW_OPENHANDS_CONTEXT",
            "CREW_PROJECT_DIR",
            "CLAUDE_PROJECT_DIR",
        ):
            env.pop(key, None)
        result = subprocess.run(
            [*argv, *extra],
            cwd=self.root / "home",
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if not success:
            self.assertNotEqual(result.returncode, 0, result.stdout)
            return result
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return json.loads(result.stdout)

    def commit_fixture(self) -> str:
        subprocess.run(
            [
                "git",
                "-C",
                str(self.root),
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "-c",
                "core.hooksPath=/dev/null",
                "commit",
                "--allow-empty",
                "-qm",
                uuid.uuid4().hex,
            ],
            check=True,
        )
        return subprocess.check_output(
            ["git", "-C", str(self.root), "rev-parse", "HEAD"], text=True
        ).strip()

    def test_real_head_guard_preserves_question_and_same_action_retry(self) -> None:
        baseline = self.commit_fixture()
        step = build.start_build(build.BuildRequest("Write hello", self.session))
        item = step.work_items[0]
        drift = self.commit_fixture()
        refused = self.run_issued(item.commands["prepare"])
        self.assertEqual(refused["authorization"], "needs_input")
        self.assertTrue(refused["never_launched"])
        self.assertEqual(refused["step"]["question"]["kind"], "workspace_guard")
        self.assertEqual(
            build.next_build(step.ref).question.question_id,
            refused["step"]["question"]["question_id"],
        )
        self.assertFalse(native._busy(self.session))
        old_intent = native.read_json(native._path(step.ref, item.action_id, "intent"))
        subprocess.run(
            ["git", "-C", str(self.root), "update-ref", "HEAD", baseline, drift],
            check=True,
        )
        ready = self.run_issued(
            refused["step"]["commands"]["decide_template"],
            "--question-id",
            refused["step"]["question"]["question_id"],
            "--kind",
            "recheck_workspace",
        )
        self.assertEqual(ready["work_items"][0]["action_id"], item.action_id)
        self.assertEqual(
            self.run_issued(ready["commands"]["resume"])["type"], "work_batch"
        )
        prepared = self.run_issued(ready["work_items"][0]["commands"]["prepare"])
        self.assertEqual(prepared["authorization"], "spawn")
        new_intent = native.read_json(native._path(step.ref, item.action_id, "intent"))
        self.assertNotEqual(new_intent["reservation_id"], old_intent["reservation_id"])
        self.assertTrue(native._busy(self.session))
        self.persist_task_result(prepared, "Done\nCREW_BUILD_STATUS: COMPLETED")
        captured = self.run_issued(item.commands["capture"])
        self.assertEqual(captured["type"], "work_batch")
        self.assertFalse(native._busy(self.session))
        self.run_issued(captured["commands"]["cancel"])

    def persist_task_result(self, prepared: dict, text: str) -> None:
        action = {
            "kind": "ActionEvent",
            "id": str(uuid.uuid4()),
            "tool_name": "task",
            "tool_call_id": str(uuid.uuid4()),
            "action": {"kind": "TaskAction", **prepared["task"]},
        }
        self.persist_event(action)
        self.persist_event(
            {
                "kind": "ObservationEvent",
                "id": str(uuid.uuid4()),
                "action_id": action["id"],
                "tool_name": "task",
                "tool_call_id": action["tool_call_id"],
                "observation": {
                    "kind": "TaskObservation",
                    "task_id": str(uuid.uuid4()),
                    "subagent": prepared["task"]["subagent_type"],
                    "status": "completed",
                    "content": [{"type": "text", "text": text}],
                },
            }
        )

    def test_emitted_parent_and_loop_review_commands_in_fresh_processes(self) -> None:
        for kind in ("review", "debate", "measure", "build"):
            with self.subTest(kind=kind):
                if kind == "debate":
                    step = review.start_debate(
                        review.DebateRequest(
                            "A subprocess debate", session_id=self.session
                        )
                    )
                    raw = review.review_step_to_dict(step)
                else:
                    ref, _item, _cancel = self.owner_fixture(kind)
                    if kind == "measure":
                        raw = measure.step_to_dict(measure.next_measure_twice(ref))
                    elif kind == "build":
                        raw = build.step_to_dict(build.next_build(ref))
                    else:
                        raw = review.review_step_to_dict(review.next_review(ref))
                for _ in range(6):
                    if raw["type"] == "terminal":
                        break
                    self.assertEqual(raw["type"], "work_batch", raw)
                    work = raw["work_items"][0]
                    commands = work.get("commands_argv", work["commands"])
                    if work["driver"] == "native":
                        prepared = self.run_issued(commands["prepare"])
                        self.assertEqual(prepared["authorization"], "spawn")
                        text = VALID
                        if work["kind"] == "advisor":
                            Path(work["staging_path"]).write_text(
                                "# Plan\nWrite and verify hello.\n"
                            )
                            text = "Plan written"
                        elif (
                            work["channel"] == "openhands"
                            and kind == "build"
                            and not work.get("review_ref")
                        ):
                            text = "Done\nCREW_BUILD_STATUS: COMPLETED"
                        elif kind == "debate":
                            text = "Independent position"
                        self.persist_task_result(prepared, text)
                        self.run_issued(commands["capture"])
                    else:
                        claimed = self.run_issued(commands["claim"])
                        self.assertEqual(claimed["authorization"], "perform")
                        returned = Path(
                            work.get("returned_path") or work["ingress_path"]
                        )
                        returned.write_text("Synthesis\nBLOCKING_CAUSES: 0\n")
                        args = [] if kind == "debate" else ["--verdict", "APPROVED"]
                        self.run_issued(
                            commands["capture"], "--returned-file", str(returned), *args
                        )
                    next_command = commands.get("next")
                    self.assertIsNotNone(next_command)
                    raw = self.run_issued(next_command)
                self.assertEqual(raw["type"], "terminal", raw)

    def test_synthesis_only_retry_emits_frozen_context_commands(self) -> None:
        step = self.start_review()
        reviewed, _ = self.complete(step.ref, step.work_items[0])
        parent = reviewed.work_items[0]
        self.run_issued(parent.commands["claim"])
        failed = self.run_issued(parent.commands["recover"], "--confirm-not-running")
        self.assertEqual(failed["outcome"]["status"], "synthesis_failed")
        retried = review.retry_review(review.RetryRequest(step.ref, ()))
        self.assertNotEqual(retried.ref.attempt_id, step.ref.attempt_id)
        parent = retried.work_items[0]
        self.assertEqual(
            self.run_issued(parent.commands["claim"])["authorization"], "perform"
        )
        returned = Path(parent.ingress_path)
        returned.write_text("Synthesis complete\nBLOCKING_CAUSES: 0\n")
        self.run_issued(
            parent.commands["capture"],
            "--returned-file",
            str(returned),
            "--verdict",
            "APPROVED",
        )
        self.assertEqual(self.run_issued(parent.commands["next"])["type"], "terminal")

    def test_waiting_recovery_command_requires_confirmation_and_retires(self) -> None:
        import shlex

        step = build.start_build(build.BuildRequest("Write hello", self.session))
        item = step.work_items[0]
        self.run_issued(item.commands["prepare"])
        waiting = self.run_issued(item.commands["next"])
        command = shlex.split(waiting["display"].split(": ", 1)[1])
        self.assertNotIn("not_running", command)
        self.assertIn("recover", command)
        self.assertNotIn("build-recover", command)
        self.run_issued(command, success=False)
        self.assertTrue(native._busy(self.session))
        recovered = self.run_issued(command, "--confirmation", "not_running")
        self.assertEqual(recovered["question"]["kind"], "execution_recovery")
        self.assertFalse(native._busy(self.session))
        self.run_issued(recovered["commands"]["cancel"])

    def test_measure_admission_refusal_preserves_owner_question(self) -> None:
        ref, item, cancel = self.owner_fixture("measure")
        measure._transaction(
            ref,
            lambda data, journal: measure._park(
                data,
                journal,
                measure._bound_question(
                    ref, "advisor_retry", "Original advisor question"
                ),
            ),
        )
        question = measure.next_measure_twice(ref).question
        refused = self.run_issued(measure.issued_argv(item)["prepare"])
        self.assertEqual(refused["authorization"], "refused")
        self.assertEqual(refused["code"], "work_not_admitted")
        self.assertTrue(refused["never_launched"])
        self.assertFalse(native._busy(self.session))
        self.assertEqual(native.recover(ref, item.action_id, "not_running"), refused)
        self.assertEqual(measure.next_measure_twice(ref).question, question)
        with self.assertRaises(review.WorkflowError) as caught:
            native.capture(ref, item.action_id)
        self.assertEqual(caught.exception.code, "claim_refused")
        cancel()

    def test_refusal_tag_after_claim_cannot_release_owned_task(self) -> None:
        ref, item, cancel = self.owner_fixture("build")
        original = build.claim_build_action

        def claimed_then_error(*args: object) -> None:
            original(*args)
            raise review.ClaimRefused("work_not_admitted", "postclaim error")

        with mock.patch.object(
            build, "claim_build_action", side_effect=claimed_then_error
        ):
            result = native.prepare(ref, item.action_id)
        self.assertEqual(result["authorization"], "do_not_spawn")
        self.assertNotIn("never_launched", result)
        self.assertTrue(native._busy(self.session))
        native.recover(ref, item.action_id, "not_running")
        cancel()

    def test_refusal_record_write_failures_remain_conservative(self) -> None:
        for after_write in (False, True):
            with self.subTest(after_write=after_write):
                ref, item, cancel = self.owner_fixture("build")
                original = measure._write_once

                def fail(
                    path: Path,
                    *args: object,
                    after_write: bool = after_write,
                    original: object = original,
                ) -> None:
                    if path.name.endswith(".refused.json"):
                        if after_write:
                            original(path, *args)
                        raise OSError("refusal persistence failure")
                    original(path, *args)

                with (
                    mock.patch.object(
                        build,
                        "claim_build_action",
                        side_effect=review.ClaimRefused(
                            "work_not_admitted", "original guard"
                        ),
                    ),
                    mock.patch.object(measure, "_write_once", side_effect=fail),
                    self.assertRaises(OSError),
                ):
                    native.prepare(ref, item.action_id)
                self.assertEqual(native._busy(self.session), not after_write)
                if after_write:
                    self.assertEqual(
                        native.prepare(ref, item.action_id)["authorization"], "spawn"
                    )
                else:
                    self.assertEqual(
                        native.prepare(ref, item.action_id)["authorization"],
                        "do_not_spawn",
                    )
                native.recover(ref, item.action_id, "not_running")
                cancel()

    def test_untyped_claim_errors_and_wait_results_never_release_reservation(
        self,
    ) -> None:
        for outcome in (
            review.WorkflowError("work_not_admitted", "uncertain write"),
            {"authorization": "wait"},
        ):
            ref, item, cancel = self.owner_fixture("build")
            if isinstance(outcome, Exception):
                with (
                    mock.patch.object(build, "claim_build_action", side_effect=outcome),
                    self.assertRaises(review.WorkflowError),
                ):
                    native.prepare(ref, item.action_id)
            else:
                with mock.patch.object(
                    build, "claim_build_action", return_value=outcome
                ):
                    self.assertEqual(
                        native.prepare(ref, item.action_id)["authorization"], "wait"
                    )
            self.assertTrue(native._busy(self.session))
            native.recover(ref, item.action_id, "not_running")
            cancel()

    def test_emitted_next_runs_without_inherited_context(self) -> None:
        for kind in ("review", "measure", "build"):
            with self.subTest(kind=kind):
                _ref, item, cancel = self.owner_fixture(kind)
                commands = (
                    measure.issued_argv(item) if kind == "measure" else item.commands
                )
                value = self.run_issued(commands["next"])
                self.assertEqual(value["type"], "work_batch")
                self.assertEqual(value["work_items"][0]["action_id"], item.action_id)
                if cancel:
                    self.assertEqual(
                        self.run_issued(value["commands"]["resume"])["type"],
                        "work_batch",
                    )
                    self.assertEqual(
                        self.run_issued(value["commands"]["cancel"])["type"], "terminal"
                    )

    def test_issued_native_actions_direct_hosts_through_prepare(self) -> None:
        for kind in ("review", "measure", "build"):
            with self.subTest(kind=kind):
                ref, item, cancel = self.owner_fixture(kind)
                commands = (
                    measure.issued_argv(item) if kind == "measure" else item.commands
                )
                self.assertNotIn("claim", commands)
                self.assertIn("prepare", commands)
                self.assertIn("openhands.py", " ".join(commands["prepare"]))
                result = subprocess.run(
                    [sys.executable, *commands["prepare"]],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["authorization"], "spawn")
                native.recover(ref, item.action_id, "not_running")
                if cancel:
                    cancel()

    def test_wrapper_rejects_both_session_and_panel_option_forms_early(self) -> None:
        for flags in (
            ["--session-id=alien"],
            ["--session-id", "alien"],
            ["--panel=full"],
            ["--panel", "full"],
        ):
            result = subprocess.run(
                [
                    sys.executable,
                    str(native.PLUGIN / "scripts/openhands.py"),
                    "run",
                    "--context-file",
                    str(self.root / "context.json"),
                    "--",
                    "review",
                    *flags,
                    "--",
                    str(self.plan),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(
                "wrapper supplies" if flags[0].startswith("--session") else "--seats",
                result.stderr,
            )
        self.assertFalse((self.root / ".crew/reviews").exists())

    def test_sdk_tool_names_and_parameters_are_admitted_and_fingerprinted(self) -> None:
        for task in ("task_tool_set", "TaskToolSet"):
            self.state["agent"]["tools"] = [
                {"name": name} for name in ("TerminalTool", "FileEditorTool", task)
            ]
            self.write_state()
            host = native.inspect_context("fixture-backend", self.conv, self.root)
            self.assertIn("TerminalTool", host.tools)
            self.state["agent"]["tools"][0]["params"] = {"working_dir": "/changed"}
            self.write_state()
            changed = native.inspect_context("fixture-backend", self.conv, self.root)
            self.assertNotEqual(changed.effective_sha256, host.effective_sha256)
        for names, code in (
            (["terminal", "file_editor", "UnknownTask"], "missing_task_tool"),
            (["UnknownTerminal", "file_editor", "task_tool_set"], "missing_tools"),
        ):
            self.state["agent"]["tools"] = [{"name": name} for name in names]
            self.write_state()
            with self.assertRaises(review.WorkflowError) as error:
                native.inspect_context("fixture-backend", self.conv, self.root)
            self.assertEqual(error.exception.code, code)

    def test_encrypted_named_credentials_refused_before_owner_admission(self) -> None:
        exported, profile, connections = self.named_package()
        original = profile.read_bytes()
        for field in (
            "api_key",
            "aws_access_key_id",
            "aws_secret_access_key",
            "aws_session_token",
        ):
            value = json.loads(original)
            value[field] = "gAAAAA-fixture-ciphertext"
            profile.write_text(json.dumps(value))
            with mock.patch.object(native, "PLUGIN", exported):
                with self.assertRaises(review.WorkflowError) as error:
                    self.start_review()
                self.assertEqual(error.exception.code, "encrypted_native_profile")
                self.assertNotIn(value[field], str(error.exception))
            self.assertFalse((self.root / ".crew/reviews").exists())
        profile.write_bytes(original)
        value = json.loads(connections.read_bytes())
        value["connections"][0]["api_key"] = "gAAAAA-fixture-ciphertext"
        connections.write_text(json.dumps(value))
        with mock.patch.object(native, "PLUGIN", exported):
            with self.assertRaises(review.WorkflowError) as error:
                self.start_review()
            self.assertEqual(error.exception.code, "encrypted_native_profile")
        self.assertFalse((self.root / ".crew/reviews").exists())

    def test_historical_settlement_retirement_after_retry(self) -> None:
        for recovery in (False, True):
            with self.subTest(recovery=recovery):
                self.plan.write_text(f"historical retirement {recovery}")
                step = self.start_review()
                item = step.work_items[0]
                with (
                    mock.patch.object(
                        native, "_retire", side_effect=OSError("interrupted retirement")
                    ),
                    self.assertRaises(OSError),
                ):
                    self.complete(step.ref, item, "failed task", status="error")
                retry = review.retry_review(review.RetryRequest(step.ref))
                self.assertNotEqual(step.ref.attempt_id, retry.ref.attempt_id)
                run = review._guard_review_path(
                    session_segment=step.ref.session_segment,
                    run_id=step.ref.run_id,
                    create=False,
                )
                before = (run / "workflow.json").read_bytes()
                self.assertTrue(native._busy(self.session))
                retired = (
                    native.recover(step.ref, item.action_id, "not_running")
                    if recovery
                    else native.capture(step.ref, item.action_id)
                )
                self.assertEqual(retired["status"], "retired")
                self.assertEqual(before, (run / "workflow.json").read_bytes())
                self.assertFalse(native._busy(self.session))
                forged = dataclasses.replace(step.ref, target_sha256="0" * 64)
                with self.assertRaises(review.WorkflowError):
                    native._owner_status(forged, item.action_id)
                with self.assertRaises(review.WorkflowError):
                    native._owner_status(retry.ref, item.action_id)
                self.assertEqual(
                    native.prepare(retry.ref, retry.work_items[0].action_id)[
                        "authorization"
                    ],
                    "spawn",
                )
                native.recover(retry.ref, retry.work_items[0].action_id, "not_running")

    def test_orphan_context_readmission_after_interrupted_start(self) -> None:
        for boundary in ("record", "workflow"):
            with self.subTest(boundary=boundary):
                self.plan.write_text(f"orphan admission {boundary}")
                patch = (
                    mock.patch.object(
                        review.review_runs,
                        "write_run_json_once",
                        side_effect=OSError("record interrupted"),
                    )
                    if boundary == "record"
                    else mock.patch.object(
                        review,
                        "_advance_locked",
                        side_effect=OSError("workflow interrupted"),
                    )
                )
                with patch, self.assertRaises(review.WorkflowError):
                    self.start_review()
                self.state["agent"]["llm"]["temperature"] = (
                    0.2 if boundary == "record" else 0.4
                )
                self.write_state()
                host = native.inspect_context("fixture-backend", self.conv, self.root)
                Path(os.environ["CREW_OPENHANDS_CONTEXT"]).write_text(
                    json.dumps(dataclasses.asdict(host))
                )
                step = self.start_review()
                self.assertEqual(native.bound_context(step.ref), host)
                native.freeze_context(step.ref, host)
                with self.assertRaises(review.WorkflowError):
                    native.admit_review_context(
                        step.ref,
                        review._guard_review_path(
                            session_segment=step.ref.session_segment,
                            run_id=step.ref.run_id,
                            create=False,
                        ),
                        dataclasses.replace(host, model="changed"),
                    )
                prepared = native.prepare(step.ref, step.work_items[0].action_id)
                self.assertEqual(prepared["authorization"], "spawn")
                native.recover(step.ref, step.work_items[0].action_id, "not_running")

    def test_unrelated_host_commands_ignore_openhands_transport_paths(self) -> None:
        ref = review.ReviewRef("codex-other", "run-example", "attempt-0001", "0" * 64)
        with mock.patch.object(
            native,
            "context_path",
            side_effect=AssertionError("unrelated transport access"),
        ):
            self.assertEqual(
                native.owner_command(ref, ("crew", "review-next")),
                ("crew", "review-next"),
            )
            self.assertEqual(
                native.owner_commands(
                    build.BuildRef("claude-other", "owner"), ("build-cancel",)
                ),
                {},
            )

    def test_standalone_stop_quiescence_requires_explicit_dispatch_to_resume(
        self,
    ) -> None:
        for debate in (False, True):
            with self.subTest(debate=debate):
                step = (
                    review.start_debate(
                        review.DebateRequest("stop debate", session_id=self.session)
                    )
                    if debate
                    else self.start_review()
                )
                item = step.work_items[0]
                native.prepare(step.ref, item.action_id)
                stopped = native.recover(step.ref, item.action_id, "not_running")
                self.assertFalse(native._busy(self.session))
                self.assertEqual(
                    native._owner_status(step.ref, item.action_id)[1], "settled"
                )
                with self.assertRaises(review.WorkflowError):
                    native.capture(step.ref, item.action_id)
                # Recovery may offer parent work, but does not claim or dispatch it.
                for pending in stopped.work_items:
                    run = review._guard_review_path(
                        session_segment=step.ref.session_segment,
                        run_id=step.ref.run_id,
                        create=False,
                    )
                    self.assertEqual(
                        review._load_action(review._workflow(run), pending.action_id)[
                            "status"
                        ],
                        "ready",
                    )
                self.assertFalse(native._busy(self.session))

    def test_event_scan_retains_conflict_detection_amid_unrelated_history(self) -> None:
        step = self.start_review()
        item = step.work_items[0]
        for _ in range(64):
            self.persist_event(
                {
                    "kind": "MessageEvent",
                    "id": str(uuid.uuid4()),
                    "message": "unrelated",
                }
            )
        with mock.patch.object(native, "capture", return_value=None):
            _, path = self.complete(step.ref, item)
        self.assertEqual(native.extract(step.ref, item.action_id)[0], VALID.encode())
        self.assertNotIsInstance(native._events(self.host), list)
        duplicate = json.loads(path.read_bytes())
        duplicate["id"] = str(uuid.uuid4())
        self.persist_event(duplicate)
        with self.assertRaises(review.WorkflowError) as error:
            native.capture(step.ref, item.action_id)
        self.assertEqual(error.exception.code, "completion_not_observed")
        self.assertTrue(native._busy(self.session))

    def test_malformed_context_and_event_shapes_are_typed(self) -> None:
        for key, value in (
            ("tools", {}),
            ("routes", []),
            ("routes", {"inherit": []}),
            ("routes", {"inherit": {"roles": []}}),
        ):
            with self.subTest(key=key):
                data = dataclasses.asdict(self.host)
                data["tools"] = list(data["tools"])
                data[key] = value
                with self.assertRaises(review.WorkflowError) as error:
                    native.parse_context(data)
                self.assertEqual(error.exception.code, "invalid_host_evidence")
        step = self.start_review()
        with mock.patch.object(native, "capture", return_value=None):
            _, path = self.complete(step.ref, step.work_items[0])
        value = json.loads(path.read_bytes())
        value["observation"] = []
        path.write_text(json.dumps(value))
        with self.assertRaises(review.WorkflowError) as error:
            native.capture(step.ref, step.work_items[0].action_id)
        self.assertEqual(error.exception.code, "invalid_host_evidence")

    def test_package_generated_parity(self) -> None:
        subprocess.run(
            [
                sys.executable,
                str(native.PLUGIN / "scripts/openhands-package.py"),
                "--check",
            ],
            check=True,
        )
        roles = {path.stem for path in (native.PLUGIN / "agents").glob("*.md")}
        packaged = {
            path.stem.removeprefix("crew-").rsplit("-", 1)[0]
            for path in (native.PLUGIN / "dev.openhands/agents").glob("*.md")
        }
        self.assertEqual(roles, packaged)
        self.assertFalse((native.PLUGIN / "dev.openhands/hooks").exists())
        version = json.loads((native.PLUGIN / "plugin.json").read_text())["version"]
        self.assertEqual(
            version,
            json.loads((native.PLUGIN / ".claude-plugin/plugin.json").read_text())[
                "version"
            ],
        )

    def change_context(self, kind: str) -> None:
        if kind == "model":
            self.state["agent"]["llm"]["model"] = "changed-model"
        elif kind == "endpoint":
            self.state["agent"]["llm"]["base_url"] = "https://changed.invalid"
        elif kind == "tools":
            self.state["agent"]["tools"].append({"name": "browser"})
        elif kind == "conversation":
            self.state["id"] = str(uuid.uuid4())
        self.write_state()
        host = native.inspect_context(
            "other-backend" if kind == "backend" else "fixture-backend",
            self.conv,
            self.root,
        )
        snapshot = native.select_context(host, new_generation=True)
        os.environ["CREW_OPENHANDS_CONTEXT"] = str(snapshot)

    def failed_review(self, *, synthesis: bool = False) -> review.ReviewStep:
        step = self.start_review()
        if synthesis:
            step, _ = self.complete(step.ref, step.work_items[0])
            item = step.work_items[0]
            review.claim_review_action(review.ClaimRequest(step.ref, item.action_id))
            return review.recover_review_action(
                review.RecoveryRequest(
                    step.ref, item.action_id, "not_running", "parent_synthesis_lost"
                )
            )
        item = step.work_items[0]
        native.prepare(step.ref, item.action_id)
        return native.recover(step.ref, item.action_id, "not_running")

    def test_retry_retains_run_context_for_native_and_parent_work(self) -> None:
        for synthesis in (False, True):
            with self.subTest(synthesis=synthesis):
                self.plan.write_text(f"Retry fixture {synthesis}")
                step = self.failed_review(synthesis=synthesis)
                old_binding = native._root(step.ref) / "context.json"
                old_bytes = old_binding.read_bytes()
                retried = review.retry_review(
                    review.RetryRequest(step.ref, () if synthesis else None)
                )
                self.assertNotEqual(step.ref.attempt_id, retried.ref.attempt_id)
                command = retried.work_items[0].commands[
                    "claim" if synthesis else "prepare"
                ]
                self.assertIn(str(old_binding), command)
                self.assertEqual(old_bytes, old_binding.read_bytes())
                self.assertNotEqual(native._root(step.ref), native._root(retried.ref))
                if synthesis:
                    self.assertEqual(
                        review.claim_review_action(
                            review.ClaimRequest(
                                retried.ref, retried.work_items[0].action_id
                            )
                        ).authorization,
                        "perform",
                    )
                else:
                    self.complete(retried.ref, retried.work_items[0])

    def test_retry_refuses_changed_generation_without_mutating_source(self) -> None:
        step = self.failed_review()
        run = review._guard_review_path(
            session_segment=step.ref.session_segment,
            run_id=step.ref.run_id,
            create=False,
        )
        before = (run / "workflow.json").read_bytes()
        binding = native._root(step.ref) / "context.json"
        retained = binding.read_bytes()
        self.change_context("model")
        with self.assertRaises(review.WorkflowError) as caught:
            review.retry_review(review.RetryRequest(step.ref))
        self.assertEqual(caught.exception.code, "host_configuration_changed")
        self.assertEqual((run / "workflow.json").read_bytes(), before)
        self.assertEqual(binding.read_bytes(), retained)

    def test_synthesis_retry_refuses_wrong_backend_without_mutation(self) -> None:
        step = self.failed_review(synthesis=True)
        run = review._guard_review_path(
            session_segment=step.ref.session_segment,
            run_id=step.ref.run_id,
            create=False,
        )
        before = (run / "workflow.json").read_bytes()
        self.change_context("backend")
        with self.assertRaises(review.WorkflowError) as caught:
            review.retry_review(review.RetryRequest(step.ref, ()))
        self.assertEqual(caught.exception.code, "session_mismatch")
        self.assertEqual((run / "workflow.json").read_bytes(), before)

    def test_parent_claim_refuses_drift_but_render_and_recovery_remain_available(
        self,
    ) -> None:
        step = self.start_review()
        step, _ = self.complete(step.ref, step.work_items[0])
        parent = step.work_items[0]
        self.state["agent"]["llm"]["model"] = "drifted"
        self.write_state()
        self.assertEqual(
            review.next_review(step.ref).work_items[0].action_id, parent.action_id
        )
        with self.assertRaises(review.WorkflowError) as caught:
            review.claim_review_action(review.ClaimRequest(step.ref, parent.action_id))
        self.assertEqual(caught.exception.code, "host_configuration_changed")
        run = review._guard_review_path(
            session_segment=step.ref.session_segment,
            run_id=step.ref.run_id,
            create=False,
        )
        self.assertEqual(
            review._load_action(review._workflow(run), parent.action_id)["status"],
            "ready",
        )

    def test_parent_capture_refuses_drift_before_writes(self) -> None:
        step = self.start_review()
        step, _ = self.complete(step.ref, step.work_items[0])
        parent = step.work_items[0]
        review.claim_review_action(review.ClaimRequest(step.ref, parent.action_id))
        self.change_context("endpoint")
        with self.assertRaises(review.WorkflowError) as caught:
            transport.capture_review_return(
                step.ref,
                parent.action_id,
                b"synthesis",
                judgment=transport.SynthesisJudgment("APPROVED"),
            )
        self.assertEqual(caught.exception.code, "host_configuration_changed")
        self.assertFalse(Path(parent.ingress_path).exists())
        self.assertFalse(Path(parent.submission_path).exists())

    def test_parent_direct_submission_refuses_wrong_conversation(self) -> None:
        step = self.start_review()
        step, _ = self.complete(step.ref, step.work_items[0], "## VERDICT\nAPPROVED\n")
        parent = step.work_items[0]
        self.assertEqual(parent.kind, "formatter")
        review.claim_review_action(review.ClaimRequest(step.ref, parent.action_id))
        Path(parent.ingress_path).write_bytes(VALID.encode())
        result = review.HostResult(
            step.ref,
            parent.action_id,
            "ok",
            {"path": parent.ingress_path, "sha256": measure.sha256(VALID.encode())},
            None,
            None,
        )
        Path(parent.submission_path).write_text(
            json.dumps(review.host_result_to_dict(result))
        )
        self.change_context("conversation")
        with self.assertRaises(review.WorkflowError) as caught:
            review.submit_review(
                review.SubmissionRequest(parent.submission_path, True, result)
            )
        self.assertEqual(caught.exception.code, "session_mismatch")
        self.assertTrue(Path(parent.submission_path).exists())

    def test_debate_successor_refuses_context_retargeting(self) -> None:
        step = review.start_debate(
            review.DebateRequest("Two rounds?", session_id=self.session, rounds=2)
        )
        closed, _ = self.complete(step.ref, step.work_items[0], "Position")
        self.assertEqual(closed.outcome["status"], "round_complete")
        self.change_context("model")
        with self.assertRaises(review.WorkflowError) as caught:
            review.next_review(step.ref)
        self.assertEqual(caught.exception.code, "host_configuration_changed")

    def test_retry_drift_matrix_preserves_run_and_binding(self) -> None:
        exported, profile, connections = self.named_package()
        with mock.patch.object(native, "PLUGIN", exported):
            host = native.inspect_context("fixture-backend", self.conv, self.root)
            snapshot = native.select_context(host)
            os.environ["CREW_OPENHANDS_CONTEXT"] = str(snapshot)
            for synthesis in (False, True):
                self.plan.write_text(f"Retry drift matrix {synthesis}")
                step = self.failed_review(synthesis=synthesis)
                run = review._guard_review_path(
                    session_segment=step.ref.session_segment,
                    run_id=step.ref.run_id,
                    create=False,
                )
                before = (run / "workflow.json").read_bytes()
                binding = native.context_path(step.ref)
                bound_bytes = binding.read_bytes()
                for kind in (
                    "model",
                    "profile",
                    "connection",
                    "backend",
                    "conversation",
                ):
                    with self.subTest(synthesis=synthesis, drift=kind):
                        original_state = json.loads(json.dumps(self.state))
                        original_profile = profile.read_bytes()
                        original_connections = connections.read_bytes()
                        try:
                            if kind == "profile":
                                value = json.loads(original_profile)
                                value["model"] = "changed-profile-model"
                                profile.write_text(json.dumps(value))
                            elif kind == "connection":
                                value = json.loads(original_connections)
                                value["connections"][0]["base_url"] = (
                                    "https://changed.invalid"
                                )
                                connections.write_text(json.dumps(value))
                            self.change_context(kind)
                            with self.assertRaises(review.WorkflowError) as caught:
                                review.retry_review(
                                    review.RetryRequest(
                                        step.ref, () if synthesis else None
                                    )
                                )
                            self.assertEqual(
                                caught.exception.code,
                                "session_mismatch"
                                if kind in {"backend", "conversation"}
                                else "host_configuration_changed",
                            )
                            self.assertEqual(
                                (run / "workflow.json").read_bytes(), before
                            )
                            self.assertEqual(binding.read_bytes(), bound_bytes)
                        finally:
                            self.state = original_state
                            self.write_state()
                            profile.write_bytes(original_profile)
                            connections.write_bytes(original_connections)
                            os.environ["CREW_OPENHANDS_CONTEXT"] = str(snapshot)

    def parent_step(self, kind: str) -> review.ReviewStep:
        self.plan.write_text(f"Parent boundary fixture {kind}")
        step = self.start_review()
        step, _ = self.complete(
            step.ref,
            step.work_items[0],
            VALID if kind == "synthesis" else "## VERDICT\nAPPROVED\n",
        )
        self.assertEqual(step.work_items[0].kind, kind)
        return step

    def test_parent_boundary_matrix_and_receipt_replay(self) -> None:
        for kind in ("formatter", "synthesis"):
            step = self.parent_step(kind)
            parent = step.work_items[0]
            run = review._guard_review_path(
                session_segment=step.ref.session_segment,
                run_id=step.ref.run_id,
                create=False,
            )
            content = VALID.encode() if kind == "formatter" else b"Synthesis complete\n"
            judgment = (
                transport.SynthesisJudgment("APPROVED") if kind == "synthesis" else None
            )
            result = review.HostResult(
                step.ref,
                parent.action_id,
                "ok",
                {"path": parent.ingress_path, "sha256": measure.sha256(content)},
                dataclasses.asdict(judgment) if judgment else None,
                None,
            )
            for boundary in ("claim", "capture", "submit"):
                if boundary == "capture":
                    self.assertEqual(
                        review.claim_review_action(
                            review.ClaimRequest(step.ref, parent.action_id)
                        ).authorization,
                        "perform",
                    )
                if boundary == "submit":
                    Path(parent.ingress_path).write_bytes(content)
                    Path(parent.submission_path).write_text(
                        json.dumps(review.host_result_to_dict(result))
                    )
                before = (run / "workflow.json").read_bytes()
                for drift in ("model", "endpoint", "tools", "backend", "conversation"):
                    with self.subTest(kind=kind, boundary=boundary, drift=drift):
                        original_state = json.loads(json.dumps(self.state))
                        original_context = os.environ["CREW_OPENHANDS_CONTEXT"]
                        try:
                            self.change_context(drift)
                            with self.assertRaises(review.WorkflowError) as caught:
                                if boundary == "claim":
                                    review.claim_review_action(
                                        review.ClaimRequest(step.ref, parent.action_id)
                                    )
                                elif boundary == "capture":
                                    transport.capture_review_return(
                                        step.ref,
                                        parent.action_id,
                                        content,
                                        judgment=judgment,
                                    )
                                else:
                                    review.submit_review(
                                        review.SubmissionRequest(
                                            parent.submission_path, True, result
                                        )
                                    )
                            self.assertEqual(
                                caught.exception.code,
                                "session_mismatch"
                                if drift in {"backend", "conversation"}
                                else "host_configuration_changed",
                            )
                            self.assertEqual(
                                (run / "workflow.json").read_bytes(), before
                            )
                            self.assertEqual(review.next_review(step.ref).ref, step.ref)
                            if boundary != "submit":
                                self.assertFalse(Path(parent.ingress_path).exists())
                                self.assertFalse(Path(parent.submission_path).exists())
                        finally:
                            self.state = original_state
                            self.write_state()
                            os.environ["CREW_OPENHANDS_CONTEXT"] = original_context
            # Direct submissions preserve caller formatting; clear only this fixture's
            # unaccepted files so deterministic capture can write its canonical envelope.
            Path(parent.submission_path).unlink()
            transport.capture_review_return(
                step.ref, parent.action_id, content, judgment=judgment
            )
            self.state["agent"]["llm"]["model"] = "drift-after-acceptance"
            self.write_state()
            transport.capture_review_return(
                step.ref, parent.action_id, content, judgment=judgment
            )
            canonical = measure._canonical(review.host_result_to_dict(result))
            Path(parent.submission_path).write_bytes(canonical)
            review.submit_review(
                review.SubmissionRequest(parent.submission_path, True, result)
            )
            with self.assertRaises(review.WorkflowError):
                transport.capture_review_return(
                    step.ref, parent.action_id, content + b"changed", judgment=judgment
                )
            self.state["agent"]["llm"]["model"] = "configured-model"
            self.write_state()

    def test_debate_successor_inherits_binding_and_renders_after_drift(self) -> None:
        step = review.start_debate(
            review.DebateRequest("Stable successor", session_id=self.session, rounds=2)
        )
        closed, _ = self.complete(step.ref, step.work_items[0], "Position")
        successor = review.next_review(step.ref)
        self.assertNotEqual(successor.ref.run_id, step.ref.run_id)
        self.assertEqual(
            native.bound_context(successor.ref), native.bound_context(step.ref)
        )
        self.state["agent"]["llm"]["model"] = "changed"
        self.write_state()
        self.assertEqual(review.next_review(closed.ref).ref, successor.ref)
        with self.assertRaises(review.WorkflowError):
            native.prepare(successor.ref, successor.work_items[0].action_id)

    def test_review_admission_binds_before_projection_and_rejects_wrong_session(
        self,
    ) -> None:
        with (
            mock.patch.object(
                review, "_work_item", side_effect=RuntimeError("projection failed")
            ),
            self.assertRaises(RuntimeError),
        ):
            self.start_review()
        bindings = list(
            (self.root / ".crew/openhands/transport").glob("*/context.json")
        )
        self.assertEqual(len(bindings), 1)
        before = bindings[0].read_bytes()
        self.change_context("model")
        step = self.start_review()
        self.assertEqual(bindings[0].read_bytes(), before)
        with self.assertRaises(review.WorkflowError):
            native.prepare(step.ref, step.work_items[0].action_id)
        self.plan.write_text("another request")
        with self.assertRaises(review.WorkflowError) as caught:
            review.start_review(
                review.ReviewRequest(str(self.plan), session_id="foreign-session")
            )
        self.assertEqual(caught.exception.code, "session_mismatch")

    def test_missing_fcntl_preserves_other_host_serialization(self) -> None:
        source = """
import builtins, sys
sys.path.insert(0, sys.argv[1])
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == 'fcntl':
        raise ImportError('fixture: no fcntl')
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from multiagent import openhands_native_transport as native
from multiagent import review_workflow as review
from multiagent import build_workflow as build
from multiagent import measure_twice as measure
ref = review.ReviewRef('claude-fixture', 'run-fixture', 'attempt-0001', 'a' * 64)
assert native.owner_command(ref, ('crew', 'review-next')) == ('crew', 'review-next')
assert build.step_to_dict(build.BuildStep(review.StepType.TERMINAL, None, ''))['type'] == 'terminal'
assert measure.step_to_dict(measure.MeasureStep(review.StepType.TERMINAL))['type'] == 'terminal'
try:
    with native._transport_lock():
        pass
except review.WorkflowError as exc:
    assert exc.code == 'unsupported_host_locking'
else:
    raise AssertionError('OpenHands must require its lock')
"""
        process = subprocess.run(
            [sys.executable, "-c", source, str(native.PLUGIN / "scripts")],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(process.returncode, 0, process.stderr)

    def test_direct_entrypoints_refuse_unsupported_python(self) -> None:
        for entry in ("openhands.py", "openhands-package.py"):
            code = "import runpy,sys; sys.version_info=(3,10); runpy.run_path(sys.argv[1],run_name='__main__')"
            process = subprocess.run(
                [sys.executable, "-c", code, str(native.PLUGIN / "scripts" / entry)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(process.returncode, 0)
            self.assertIn("Python 3.11+", process.stderr)
            self.assertNotIn("Traceback", process.stderr)

    def test_nested_parent_retry_refuses_before_owner_mutation(self) -> None:
        import loop_state

        for kind in ("build", "measure"):
            with self.subTest(owner=kind):
                if kind == "build":
                    step = build.start_build(
                        build.BuildRequest("Write nested fixture", self.session)
                    )
                    item = step.work_items[0]
                    step, _ = self.complete(
                        step.ref,
                        item,
                        "Done\nCREW_BUILD_STATUS: COMPLETED",
                        callback=lambda: (self.root / "nested.txt").write_text(
                            "nested"
                        ),
                    )
                    advance = build.next_build
                    cancel = build.cancel_build
                else:
                    step = measure.start_measure_twice(
                        measure.MeasureRequest("requirements.md", self.session)
                    )
                    item = step.work_items[0]
                    step, _ = self.complete(
                        step.ref,
                        item,
                        "Plan written",
                        callback=lambda item=item: Path(item.staging_path).write_text(
                            "# Plan\nNested fixture"
                        ),
                    )
                    advance = measure.next_measure_twice
                    cancel = measure.cancel_measure_twice
                reviewer = step.work_items[0]
                self.assertEqual(
                    native.bound_context(step.ref),
                    native.bound_context(reviewer.review_ref),
                )
                self.complete(reviewer.review_ref, reviewer.review_item)
                parent = advance(step.ref).work_items[0]
                original_context = os.environ["CREW_OPENHANDS_CONTEXT"]
                self.change_context("backend")
                with self.assertRaises(review.WorkflowError) as caught:
                    review.claim_review_action(
                        review.ClaimRequest(parent.review_ref, parent.action_id)
                    )
                self.assertEqual(caught.exception.code, "session_mismatch")
                os.environ["CREW_OPENHANDS_CONTEXT"] = original_context
                review.claim_review_action(
                    review.ClaimRequest(parent.review_ref, parent.action_id)
                )
                self.state["agent"]["llm"]["model"] = "drift-before-nested-capture"
                self.write_state()
                with self.assertRaises(review.WorkflowError):
                    transport.capture_review_return(
                        parent.review_ref,
                        parent.action_id,
                        b"Synthesis",
                        judgment=transport.SynthesisJudgment("APPROVED"),
                    )
                review.recover_review_action(
                    review.RecoveryRequest(
                        parent.review_ref,
                        parent.action_id,
                        "not_running",
                        "parent_synthesis_lost",
                    )
                )
                self.state["agent"]["llm"]["model"] = "configured-model"
                self.write_state()
                wait = advance(step.ref)
                self.assertEqual(wait.question.kind, "synthesis_retry")
                decision = (
                    build.BuildDecision if kind == "build" else measure.MeasureDecision
                )(wait.ref, wait.question.question_id, "retry_synthesis")
                owner_file = loop_state.resolve(
                    "bl" if kind == "build" else "mt", self.session
                )
                before = owner_file.read_bytes()
                self.change_context("model")
                decide = (
                    (lambda decision=decision: build.decide_build(decision))
                    if kind == "build"
                    else (
                        lambda ref=wait.ref, decision=decision: (
                            measure.decide_measure_twice(ref, decision)
                        )
                    )
                )
                with self.assertRaises(review.WorkflowError) as caught:
                    decide()
                self.assertEqual(caught.exception.code, "host_configuration_changed")
                self.assertEqual(owner_file.read_bytes(), before)
                self.state["agent"]["llm"]["model"] = "configured-model"
                self.write_state()
                os.environ["CREW_OPENHANDS_CONTEXT"] = original_context
                retry = decide()
                self.assertEqual(
                    retry.work_items[0].review_ref.attempt_id, "attempt-0002"
                )
                self.assertEqual(
                    native.bound_context(retry.work_items[0].review_ref),
                    native.bound_context(wait.ref),
                )
                cancel(step.ref, "fixture done")


if __name__ == "__main__":
    unittest.main()
