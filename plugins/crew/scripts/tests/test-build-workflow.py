#!/usr/bin/env python3
"""Build contracts through public APIs, real CLI parsing and isolated repositories."""

from __future__ import annotations

# ruff: noqa: B023 - injected callbacks run synchronously within each subtest
import concurrent.futures
import contextlib
import dataclasses
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import artifact_prune
import build_state
import loop_state
import models
from multiagent import (
    build_workflow as build,
)
from multiagent import (
    claude_native_transport as native,
)
from multiagent import (
    cli,
    config,
    continuations,
    execution,
)
from multiagent import (
    review_workflow as review,
)
from multiagent import (
    workflow_transport as transport,
)
from multiagent.providers import ContinuationOutcome, ProviderResult

VALID = b"## VERDICT\nAPPROVED\n\n## FINDINGS\nnone\n"
COMPLETED = b"Implemented\r\nCREW_BUILD_STATUS: COMPLETED\r\n"


class BuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.env = mock.patch.dict(
            os.environ,
            {
                "CLAUDE_PROJECT_DIR": str(self.root),
                "CREW_HOST": "claude",
                "CLAUDE_WORKING_DIRECTORY": str(self.root),
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        home_patch = mock.patch("pathlib.Path.home", return_value=self.home)
        home_patch.start()
        self.addCleanup(home_patch.stop)
        config._reset_cache_for_tests()
        self.addCleanup(config._reset_cache_for_tests)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / ".gitignore").write_text(".crew/\nhome/\n")
        self.request = build.BuildRequest(
            "--seats opus,sonnet --executor crew:executor Write hello.txt",
            "isolated-build",
        )

    def state(self) -> dict:
        return loop_state.read(loop_state.resolve("bl", self.request.session_id))

    def start(self) -> build.BuildStep:
        return build.start_build(self.request)

    def draft(self, content: bytes = COMPLETED) -> build.BuildStep:
        step = self.start()
        item = step.work_items[0]
        self.assertEqual(
            build.claim_build_action(step.ref, item.action_id)["authorization"], "spawn"
        )
        (self.root / "hello.txt").write_text("hello\n")
        return build.capture_build_return(step.ref, item.action_id, content)

    def panel(
        self,
        step: build.BuildStep,
        verdict: str = "APPROVED",
        minor: bool = False,
        failed: int = 0,
        synthesis_fail: bool = False,
    ) -> build.BuildStep:
        for index, item in enumerate(step.work_items):
            review.claim_review_action(
                review.ClaimRequest(item.review_ref, item.action_id)
            )
            transport.capture_review_return(
                item.review_ref,
                item.action_id,
                VALID,
                status="failed" if index < failed else "ok",
                diagnostic="outage" if index < failed else None,
            )
        step = build.next_build(step.ref)
        if step.work_items and step.work_items[0].kind == "synthesis":
            item = step.work_items[0]
            review.claim_review_action(
                review.ClaimRequest(item.review_ref, item.action_id)
            )
            transport.capture_review_return(
                item.review_ref,
                item.action_id,
                b"All findings assessed\nBLOCKING_CAUSES: 0\n",
                status="failed" if synthesis_fail else "ok",
                diagnostic="synthesis lost" if synthesis_fail else None,
                judgment=None
                if synthesis_fail
                else transport.SynthesisJudgment(verdict, minor),
            )
            step = build.next_build(step.ref)
        return step

    def decision(
        self, step: build.BuildStep, kind: str, **fields: str
    ) -> build.BuildStep:
        return build.decide_build(
            build.BuildDecision(step.ref, step.question.question_id, kind, **fields)
        )

    def test_request_grammar_preserves_task_bytes(self) -> None:
        self.assertEqual(build.parse_arguments(" \t task\n").task, " \t task\n")
        self.assertEqual(
            build.parse_arguments(
                '--executor "crew:executor" --panel quick task\r\n'
            ).task,
            "task\r\n",
        )
        for raw in (
            "--panel",
            "--panel x --panel y task",
            "--bogus x task",
            "--executor=sol task",
            "--seats --executor sol task",
            "",
            "\x00",
        ):
            with self.subTest(raw=raw), self.assertRaises(review.WorkflowError):
                build.parse_arguments(raw)

    def test_terminal_marker_exact_bytes(self) -> None:
        for ending in (b"", b"\n", b"\r\n", b"\n \t\n"):
            self.assertEqual(
                build.report_status(b"CREW_BUILD_STATUS: COMPLETED" + ending),
                "completed",
            )
        self.assertEqual(
            build.report_status(
                b"example CREW_BUILD_STATUS: BLOCKED\nCREW_BUILD_STATUS: COMPLETED"
            ),
            "completed",
        )
        self.assertEqual(
            build.report_status(b"CREW_BUILD_STATUS: BLOCKED\r"), "blocked"
        )
        for text in (
            b"",
            b'"CREW_BUILD_STATUS: COMPLETED"',
            b" CREW_BUILD_STATUS: COMPLETED",
            b"CREW_BUILD_STATUS: COMPLETED ",
            b"CREW_BUILD_STATUS: COMPLETED\n```",
            b"CREW_BUILD_STATUS: UNKNOWN",
            b"CREW_BUILD_STATUS: COMPLETED\xff",
            "prefix\u2028CREW_BUILD_STATUS: COMPLETED".encode(),
        ):
            with self.subTest(text=text):
                self.assertEqual(build.report_status(text), "invalid_report")

    def test_completion_and_terminal_replay(self) -> None:
        done = self.panel(self.draft())
        self.assertEqual(done.type, "terminal")
        self.assertEqual(done.outcome["status"], "approved")
        self.assertEqual(build.next_build(done.ref), done)
        self.assertEqual(build.resume_build(self.request.session_id), done)
        self.assertFalse(self.state()["active"])
        old = done.ref
        fresh = self.start()
        self.assertNotEqual(fresh.ref, old)
        before = self.state()
        with self.assertRaises(review.WorkflowError):
            build.next_build(old)
        self.assertEqual(self.state(), before)

    def test_done_checkpoint_survives_failed_deactivation(self) -> None:
        step = self.draft()
        with (
            mock.patch.object(
                loop_state,
                "deactivate",
                side_effect=OSError("injected interruption before finalization"),
            ),
            self.assertRaises(OSError),
        ):
            self.panel(step)
        checkpoint = self.state()
        self.assertTrue(checkpoint["active"])
        self.assertEqual(checkpoint["phase"], "done")
        self.assertEqual(checkpoint["last_verdict"], "APPROVED")
        self.assertEqual(checkpoint["bl_workflow"]["stage"], "done")
        self.assertEqual(len(checkpoint["bl_workflow"]["applied_outcomes"]), 1)
        with (
            mock.patch.object(execution, "execute_write", side_effect=AssertionError),
            mock.patch.object(
                review, "execute_external_review", side_effect=AssertionError
            ),
            mock.patch.object(loop_state, "bound_reason", side_effect=AssertionError),
        ):
            done = build.next_build(step.ref)
            self.assertEqual(done.type, "terminal")
            self.assertEqual(done.outcome["status"], "approved")
            self.assertEqual(build.next_build(step.ref), done)
        final = self.state()
        self.assertFalse(final["active"])
        self.assertEqual(
            final["bl_workflow"]["applied_outcomes"],
            checkpoint["bl_workflow"]["applied_outcomes"],
        )
        self.assertEqual(final["revision_round"], checkpoint["revision_round"])

    def test_provider_launch_and_guards_use_the_explicit_workspace(self) -> None:
        from multiagent.providers import agy, codex, cursor

        other = self.root / "shell-cwd"
        other.mkdir()
        for module, provider in (
            (codex, codex.CodexProvider("sol", "gpt-test")),
            (cursor, cursor.CursorProvider("cursor-auto", "auto")),
            (agy, agy.AgyProvider("agy-test", "gemini-test")),
        ):
            with self.subTest(provider=module.__name__):
                calls = []

                def launch(argv: list, **options: object) -> tuple[int, str, str]:
                    calls.append(options["cwd"])
                    # Real child filesystem work, with no provider or paid process.
                    subprocess.run(
                        [
                            sys.executable,
                            "-c",
                            "from pathlib import Path; Path('launch.txt').write_text('here')",
                        ],
                        cwd=options["cwd"],
                        check=True,
                    )
                    return 0, COMPLETED.decode(), ""

                with (
                    mock.patch.dict(os.environ, {"CLAUDE_WORKING_DIRECTORY": ""}),
                    mock.patch.object(module, "run_reaped", side_effect=launch),
                    mock.patch.object(
                        provider, "is_available", return_value=(True, "")
                    ),
                    mock.patch.object(module.os, "getcwd", return_value=str(other)),
                ):
                    envelope = execution.execute_write(
                        execution.WriteRequest(
                            "sol",
                            "Write launch.txt",
                            None,
                            30,
                            {},
                            str(self.root),
                            "workspace-test",
                            None,
                            "",
                        ),
                        provider=provider,
                    )
                self.assertEqual(calls, [str(self.root)])
                self.assertTrue((self.root / "launch.txt").exists())
                self.assertFalse((other / "launch.txt").exists())
                self.assertFalse(envelope["head_moved"])
                (self.root / "launch.txt").unlink()

    def test_corrupt_writer_evidence_survives_stop_and_refuses_all_starts(self) -> None:
        scripts = Path(__file__).resolve().parents[1]
        for external in (False, True):
            with self.subTest(external=external):
                self.request = build.BuildRequest(
                    "--seats opus --executor "
                    + ("sol" if external else "crew:executor")
                    + " task",
                    "corrupt-external" if external else "corrupt-native",
                )
                with mock.patch.object(
                    config, "build_resume_executor", return_value=False
                ):
                    step = self.start()
                build._claim(
                    step.ref,
                    step.work_items[0].action_id,
                    execution.observe_workspace(str(self.root)),
                    external=external,
                )
                state = loop_state.resolve("bl", self.request.session_id)
                state.write_text('{"bl_workflow": {"outstanding_writer":')
                before = state.read_bytes()
                env = {
                    **os.environ,
                    "HOME": str(self.home),
                    "CLAUDE_SESSION_ID": self.request.session_id,
                }
                result = subprocess.run(
                    [sys.executable, str(scripts / "persistent-mode.py")],
                    input=json.dumps(
                        {"session_id": self.request.session_id, "cwd": str(self.root)}
                    ),
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(
                    "Unreadable build writer state", result.stdout + result.stderr
                )
                for loop in ("bl", "mt"):
                    result = subprocess.run(
                        [
                            sys.executable,
                            str(scripts / "crew-state.py"),
                            "init",
                            loop,
                            "--prompt" if loop == "bl" else "--task",
                            "replacement",
                            "--session-id",
                            self.request.session_id,
                        ],
                        env=env,
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=10,
                    )
                    self.assertEqual(
                        result.returncode, 2, result.stdout + result.stderr
                    )
                    self.assertIn("unreadable build writer state", result.stderr)
                with self.assertRaises(
                    (loop_state.LoopStateError, execution.ExecutionError)
                ):
                    self.start()
                self.assertEqual(state.read_bytes(), before)
                self.assertFalse(state.with_suffix(".json.corrupt").exists())

    def test_cleanup_rechecks_progress_mtime_and_active_policy_under_lock(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "cleanup_progress_barrier",
            Path(__file__).resolve().parents[1] / "session-start.py",
        )
        hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook)
        for was_active in (True, False):
            with self.subTest(was_active=was_active):
                self.request = dataclasses.replace(
                    self.request, session_id="progress-" + str(was_active)
                )
                self.start()
                path = loop_state.resolve("bl", self.request.session_id)
                data = self.state()
                data["active"] = was_active
                models.atomic_write_json(path, data)
                old = time.time() - (10 if was_active else 2) * 86400
                os.utime(path, (old, old))
                reached, release = threading.Event(), threading.Event()
                original = hook.state_lock

                @contextlib.contextmanager
                def barrier(candidate: Path) -> Iterator[None]:
                    if candidate == path:
                        reached.set()
                        self.assertTrue(release.wait(3))
                    with original(candidate):
                        yield

                with (
                    concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool,
                    mock.patch.object(hook, "state_lock", side_effect=barrier),
                ):
                    pending = pool.submit(hook.cleanup_stale_files, self.root / ".crew")
                    self.assertTrue(reached.wait(3))
                    # Same-owner progress takes only its own lock, no admission.
                    with models.state_lock(path):
                        data["active"] = True
                        models.atomic_write_json(path, data)
                        if not was_active:
                            os.utime(path, (old, old))
                        before = path.read_bytes()
                    release.set()
                    pending.result(3)
                self.assertEqual(path.read_bytes(), before)

    def test_prompt_write_failure_recovers_without_reapplying_review(self) -> None:
        step = self.draft()
        original = build._write_once

        def fail_prompt(path: Path, content: bytes, ref: build.BuildRef) -> None:
            if path.parent.name == "prompts":
                raise OSError("temporary publication failure")
            original(path, content, ref)

        with mock.patch.object(build, "_write_once", side_effect=fail_prompt):
            step = self.panel(step, "REVISE")
        self.assertEqual(step.question.kind, "feedback_recovery")
        before = self.state()
        self.assertIsNone(before["bl_workflow"]["round_prompt_path"])
        step = self.decision(step, "retry_feedback")
        self.assertEqual(step.work_items[0].kind, "revision")
        self.assertTrue(Path(step.work_items[0].prompt_path).is_file())
        after = self.state()
        self.assertEqual(after["revision_round"], before["revision_round"])
        self.assertEqual(
            after["bl_workflow"]["applied_outcomes"],
            before["bl_workflow"]["applied_outcomes"],
        )
        self.assertEqual(
            after["bl_workflow"]["review_generation"],
            before["bl_workflow"]["review_generation"],
        )

    def test_revision_feedback_accepts_workspace_alias_but_rejects_namespace_links(
        self,
    ) -> None:
        ancestor = self.home / "workspace-parent"
        ancestor.symlink_to(self.root.parent, target_is_directory=True)
        alias = ancestor / self.root.name
        with mock.patch.dict(
            os.environ,
            {"CLAUDE_PROJECT_DIR": str(alias), "CLAUDE_WORKING_DIRECTORY": str(alias)},
        ):
            step = self.panel(self.draft(), "REVISE")
            self.assertEqual(step.work_items[0].kind, "revision")
            feedback = self.state()["bl_workflow"]["feedback_paths"][0]
            canonical = build._safe_feedback(feedback)
            self.assertTrue(canonical.is_relative_to(self.root / ".crew/reviews"))
            link = canonical.with_name("feedback-link.md")
            link.symlink_to(canonical)
            with self.assertRaises(review.WorkflowError):
                build._safe_feedback(str(link))
            with self.assertRaises(review.WorkflowError):
                build._safe_feedback(
                    str(
                        canonical.parent / ".." / canonical.parent.name / canonical.name
                    )
                )
            canonical.write_bytes(b"\xff")
            build._transaction(
                step.ref,
                lambda _data, j: (
                    setattr(j, "action", None),
                    setattr(j, "round_prompt_path", None),
                    setattr(j, "round_prompt_sha256", None),
                ),
            )
            parked = build.next_build(step.ref)
            self.assertEqual(parked.question.kind, "feedback_recovery")
            canonical.write_text("Repaired retained findings")
            self.assertEqual(
                self.decision(parked, "retry_feedback").work_items[0].kind, "revision"
            )

    def test_guarded_claim_projects_input_without_launch_on_both_routes(self) -> None:
        for external in (False, True):
            with self.subTest(external=external):
                self.request = build.BuildRequest(
                    "--seats opus --executor "
                    + ("sol" if external else "crew:executor")
                    + " task",
                    "claim-" + str(external),
                )
                step = self.start()
                baseline = execution.observe_workspace(str(self.root))
                subprocess.run(
                    ["git", "symbolic-ref", "HEAD", "refs/heads/changed"],
                    cwd=self.root,
                    check=True,
                )
                if external:
                    with mock.patch.object(build, "get_provider") as provider:
                        provider.return_value.effective_timeout = None
                        parked = build.execute_build_action(
                            step.ref, step.work_items[0].action_id
                        )
                        provider.return_value.run.assert_not_called()
                else:
                    result = build.claim_build_action(
                        step.ref, step.work_items[0].action_id
                    )
                    self.assertEqual(result["authorization"], "needs_input")
                    self.assertEqual(result["step"]["type"], "needs_input")
                    parked = build.next_build(step.ref)
                self.assertEqual(parked.question.kind, "workspace_guard")
                self.assertFalse(parked.in_flight)
                self.assertIsNone(self.state()["bl_workflow"]["outstanding_writer"])
                with self.assertRaisesRegex(
                    review.WorkflowError, "current issued question"
                ):
                    build.decide_build(
                        build.BuildDecision(parked.ref, "0" * 64, "recheck_workspace"),
                    )
                subprocess.run(
                    ["git", "symbolic-ref", "HEAD", "refs/heads/" + baseline.branch],
                    cwd=self.root,
                    check=True,
                )
                self.assertEqual(
                    self.decision(parked, "recheck_workspace").type, "work_batch"
                )

    def test_matched_wait_consumes_request_but_unadmitted_start_retains_it(
        self,
    ) -> None:
        step = self.start()
        build.claim_build_action(step.ref, step.work_items[0].action_id)
        requests = self.root / ".crew/requests"
        requests.mkdir()
        spill = requests / "start.json"
        spill.write_text(
            json.dumps({"schema": 1, "raw_arguments": self.request.raw_arguments})
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                cli.main(
                    [
                        "build",
                        "-f",
                        str(spill),
                        "--session-id",
                        self.request.session_id,
                        "--consume",
                    ]
                ),
                0,
            )
        self.assertFalse(spill.exists())
        spill.write_text(
            json.dumps({"schema": 1, "raw_arguments": self.request.raw_arguments})
        )
        with (
            mock.patch.object(
                continuations,
                "continuation_lock",
                side_effect=continuations.ContinuationLockError("busy"),
            ),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(
                cli.main(
                    [
                        "build",
                        "-f",
                        str(spill),
                        "--session-id",
                        "unadmitted",
                        "--consume",
                    ]
                ),
                0,
            )
        self.assertTrue(spill.exists())
        result = json.loads(output.getvalue())
        self.assertIsNone(result["ref"])
        self.assertFalse(result["work_items"])
        self.assertIn("retry the same build request", result["display"])

    def test_swab_batch_scan_and_partial_lock_failure_preserve_pointer_hygiene(
        self,
    ) -> None:
        session = self.root / ".crew/reviews/swab"
        items = []
        for name in ("run-000000000001", "run-000000000002"):
            path = session / name
            path.mkdir(parents=True)
            items.append(artifact_prune.Prunable("review-run", path, name, 0, session))
        (session / "current-run.json").write_text(json.dumps({"run_id": items[0].name}))
        import shutil

        original = shutil.rmtree

        def remove(path: str, *args: object, **kwargs: object) -> None:
            if Path(path) == items[1].path:
                raise models.StateLockError("held owner")
            original(path, *args, **kwargs)

        with (
            mock.patch.object(
                artifact_prune, "collect_prunable", return_value=items
            ) as scans,
            mock.patch.object(cli.shutil, "rmtree", side_effect=remove),
            contextlib.redirect_stdout(io.StringIO()) as output,
            contextlib.redirect_stderr(io.StringIO()) as errors,
        ):
            self.assertEqual(cli.main(["swab", "--yes", "--json"]), 1)
        report = json.loads(output.getvalue())
        self.assertEqual(len(report["removed"]), 1)
        self.assertEqual(len(report["failed"]), 1)
        self.assertIn("locked, retry", errors.getvalue())
        self.assertFalse((session / "current-run.json").exists())
        self.assertTrue(items[1].path.exists())
        self.assertEqual(scans.call_count, 2)

    def test_swab_admission_lock_timeout_returns_failed_list(self) -> None:
        directory = self.root / ".crew"
        directory.mkdir()
        path = directory / "probe-test.json"
        path.write_text("evidence")
        item = artifact_prune.Prunable("probe", path, path.name, 8, directory)
        with (
            mock.patch.object(artifact_prune, "collect_prunable", return_value=[item]),
            mock.patch.object(
                artifact_prune,
                "state_lock",
                side_effect=models.StateLockError("busy admission"),
            ),
            contextlib.redirect_stdout(io.StringIO()) as output,
            contextlib.redirect_stderr(io.StringIO()) as errors,
        ):
            self.assertEqual(cli.main(["swab", "--yes", "--json"]), 1)
        report = json.loads(output.getvalue())
        self.assertFalse(report["removed"])
        self.assertEqual(len(report["failed"]), 1)
        self.assertIn("locked, retry", errors.getvalue())
        self.assertTrue(path.exists())

    def test_native_guard_parks_before_review(self) -> None:
        step = self.start()
        original_branch = execution.observe_workspace(str(self.root)).branch
        build.claim_build_action(step.ref, step.work_items[0].action_id)
        subprocess.run(
            ["git", "symbolic-ref", "HEAD", "refs/heads/changed"],
            cwd=self.root,
            check=True,
        )
        step = build.capture_build_return(
            step.ref, step.work_items[0].action_id, COMPLETED
        )
        self.assertEqual(step.question.kind, "workspace_guard")
        self.assertEqual(self.state()["phase"], "drafting")
        with self.assertRaises(review.WorkflowError):
            self.decision(step, "force", confirmation="force")
        subprocess.run(
            ["git", "symbolic-ref", "HEAD", "refs/heads/" + original_branch],
            cwd=self.root,
            check=True,
        )
        self.assertEqual(
            self.decision(step, "recheck_workspace").work_items[0].kind, "reviewer"
        )

    def test_blocked_answer_changes_only_new_round_prompt(self) -> None:
        step = self.draft(b"Need guidance\nCREW_BUILD_STATUS: BLOCKED\n")
        self.assertEqual(step.question.kind, "executor_blocked")
        old = self.state()
        answer = build.BuildDecision(
            step.ref,
            step.question.question_id,
            "answer_executor",
            "stack_edits",
            step.question.workspace_sha256,
            answer="Use the existing format.",
        )
        step = build.decide_build(answer)
        self.assertEqual(
            self.state()["bl_workflow"]["request_id"], old["bl_workflow"]["request_id"]
        )
        self.assertEqual(self.state()["started_at"], old["started_at"])
        prompt = Path(step.work_items[0].prompt_path).read_text()
        self.assertIn("Use the existing format.", prompt)
        feedback = build._root(step.ref) / "returns" / "action-0001.txt"
        self.assertIn(f"Read retained full feedback file {feedback}", prompt)
        self.assertEqual(
            feedback.read_bytes(), b"Need guidance\nCREW_BUILD_STATUS: BLOCKED\n"
        )
        self.assertEqual(build.decide_build(answer), step)
        with self.assertRaises(review.WorkflowError):
            build.decide_build(dataclasses.replace(answer, answer="Changed"))
        answered = self.state()["bl_workflow"]
        self.assertNotEqual(
            answered["round_prompt_sha256"], old["bl_workflow"]["round_prompt_sha256"]
        )
        self.assertEqual(answered["round_prompt_sha256"], build.sha256(prompt.encode()))
        action = step.work_items[0].action_id
        build.claim_build_action(step.ref, action)
        step = build.capture_build_return(step.ref, action, COMPLETED)
        step = self.panel(step, "REVISE")
        revised = self.state()["bl_workflow"]
        self.assertNotEqual(
            revised["round_prompt_sha256"], answered["round_prompt_sha256"]
        )
        self.assertEqual(revised["round_prompt_path"], step.work_items[0].prompt_path)
        self.assertEqual(
            revised["round_prompt_sha256"],
            build.sha256(Path(step.work_items[0].prompt_path).read_bytes()),
        )

    def test_invalid_report_never_auto_retries_or_reviews(self) -> None:
        step = self.draft(b"completed without a terminal marker")
        self.assertEqual(step.question.kind, "executor_report_recovery")
        self.assertEqual(self.state()["bl_workflow"]["review_generation"], 0)
        step = self.decision(
            step,
            "retry_executor",
            confirmation="stack_edits",
            workspace_sha256=step.question.workspace_sha256,
        )
        self.assertIn(
            "invalid terminal marker", Path(step.work_items[0].prompt_path).read_text()
        )

    def test_claim_once_and_uncertain_recovery(self) -> None:
        step = self.start()
        action_id = step.work_items[0].action_id
        with concurrent.futures.ThreadPoolExecutor() as pool:
            results = list(
                pool.map(
                    lambda _: build.claim_build_action(step.ref, action_id), range(2)
                )
            )
        self.assertEqual(sorted(r["authorization"] for r in results), ["spawn", "wait"])
        self.assertEqual(build.next_build(step.ref).type, "waiting")
        with self.assertRaises(review.WorkflowError):
            build.recover_build_action(step.ref, action_id, "probably")
        recovered = build.recover_build_action(step.ref, action_id, "not_running")
        self.assertEqual(recovered.question.kind, "execution_recovery")
        adopted = self.decision(
            recovered,
            "adopt_edits",
            confirmation="completed",
            completed_action=action_id,
            workspace_sha256=recovered.question.workspace_sha256,
        )
        self.assertEqual(adopted.work_items[0].kind, "reviewer")

    def test_cancelled_writer_fences_build_mt_and_init(self) -> None:

        step = self.start()
        action = step.work_items[0].action_id
        build.claim_build_action(step.ref, action)
        cancelled = build.cancel_build(step.ref)
        self.assertIn("build-recover", cancelled.outcome["writer_fence"])
        with self.assertRaises(loop_state.LoopStateError):
            self.start()
        with self.assertRaises(loop_state.LoopStateError):
            loop_state.initialize("task", self.request.session_id, journal={})
        build.recover_build_action(step.ref, action, "not_running")
        self.assertFalse(self.state()["active"])
        self.assertNotEqual(self.start().ref, step.ref)

    def test_journal_roundtrip_and_strict_unknown_fields(self) -> None:
        self.start()
        value = self.state()["bl_workflow"]
        self.assertEqual(build.journal_to_dict(build.journal_from_dict(value)), value)
        for field in ("version", "selection", "policy", "action", "executor"):
            malformed = json.loads(json.dumps(value))
            if field == "version":
                malformed[field] = True
            else:
                malformed[field]["unknown"] = 1
            with self.subTest(field=field), self.assertRaises(review.WorkflowError):
                build.journal_from_dict(malformed)

    def test_resume_matching_stamp_and_conflicting_request(self) -> None:
        step = self.start()
        retained = build.start_build(
            dataclasses.replace(
                self.request,
                raw_arguments=self.request.raw_arguments.replace(
                    "crew:executor", "sol"
                ),
            )
        )
        self.assertEqual(retained.ref, step.ref)
        self.assertEqual(retained.work_items, step.work_items)
        self.assertIn("Active executor stamp", retained.display)
        before = self.state()
        with self.assertRaises(review.WorkflowError):
            build.start_build(
                dataclasses.replace(
                    self.request, raw_arguments=self.request.raw_arguments + " "
                )
            )
        self.assertEqual(self.state(), before)

    def test_external_retry_counts_and_prompt_identity(self) -> None:
        for retries in range(3):
            with self.subTest(retries=retries):
                self.request = build.BuildRequest(
                    "--seats opus --executor sol Write file", f"retry-{retries}"
                )
                calls = []

                class Provider:
                    supports_continuation = False

                    def is_available(self) -> tuple[bool, str]:
                        return True, ""

                    def run(inner, prompt: str, **options: object) -> ProviderResult:
                        calls.append((prompt, options))
                        p = self.root / "stack.txt"
                        p.write_text(
                            p.read_text() + "edit\n" if p.exists() else "edit\n"
                        )
                        return ProviderResult(
                            "sol", "model", False, "partial", "failed", 0.01
                        )

                with (
                    mock.patch.object(
                        config, "build_executor_retries", return_value=retries
                    ),
                    mock.patch.object(build, "get_provider", return_value=Provider()),
                ):
                    step = self.start()
                    for _ in range(retries + 1):
                        step = build.execute_build_action(
                            step.ref, step.work_items[0].action_id
                        )
                self.assertEqual(len(calls), retries + 1)
                self.assertEqual(len({prompt for prompt, _ in calls}), 1)
                self.assertEqual(step.question.kind, "execution_recovery")
                self.assertEqual(
                    self.state()["bl_workflow"]["round_attempt"], retries + 1
                )
                journal = self.state()["bl_workflow"]
                self.assertEqual(
                    journal["round_prompt_sha256"], build.sha256(calls[0][0].encode())
                )
                self.assertTrue(
                    journal["round_prompt_path"].endswith("action-0001.txt")
                )

    def test_frozen_round_prompt_drift_refuses_automatic_retry(self) -> None:
        for damage in ("changed", "missing", "symlink", "directory"):
            with self.subTest(damage=damage):
                self.request = build.BuildRequest(
                    "--executor sol --seats opus task", f"round-drift-{damage}"
                )
                provider = mock.Mock(supports_continuation=False)
                provider.is_available.return_value = (True, "")
                provider.effective_timeout.side_effect = lambda value: value
                original = b""
                prompt = None

                def run(text: str, **options: object) -> ProviderResult:
                    nonlocal original, prompt
                    if provider.run.call_count == 1:
                        prompt = Path(self.state()["bl_workflow"]["round_prompt_path"])
                        original = prompt.read_bytes()
                        if damage == "changed":
                            prompt.write_bytes(original + b"Unauthorized instruction\n")
                        else:
                            prompt.unlink()
                            if damage == "symlink":
                                foreign = self.home / "foreign-prompt.txt"
                                foreign.write_bytes(original)
                                prompt.symlink_to(foreign)
                            elif damage == "directory":
                                prompt.mkdir()
                        return ProviderResult(
                            "sol", options["model"], False, "partial", "failed", 0.0
                        )
                    self.assertEqual(text.encode(), original)
                    return ProviderResult(
                        "sol", options["model"], True, COMPLETED.decode(), None, 0.0
                    )

                provider.run.side_effect = run
                with (
                    mock.patch.object(build, "get_provider", return_value=provider),
                    mock.patch.object(
                        config, "build_resume_executor", return_value=False
                    ),
                    mock.patch.object(config, "build_executor_retries", return_value=1),
                ):
                    step = self.start()
                    before = self.state()
                    item = step.work_items[0]
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        code = cli.main(list(item.commands["execute"][1:]))
                    response = json.loads(output.getvalue())
                    self.assertEqual(code, 0 if damage == "missing" else 2)
                    if code == 0:
                        self.assertEqual(
                            response["question"]["kind"], "feedback_recovery"
                        )
                    else:
                        self.assertIn(
                            response["code"],
                            {"conflict", "unsafe_path", "unsafe_plan_path"},
                        )
                    current = self.state()
                    journal = current["bl_workflow"]
                    self.assertEqual(provider.run.call_count, 1)
                    self.assertIsNone(journal["action"])
                    self.assertIsNone(journal["outstanding_writer"])
                    self.assertEqual(journal["action_ordinal"], 1)
                    self.assertEqual(journal["round_attempt"], 1)
                    self.assertEqual(
                        journal["round_prompt_sha256"], build.sha256(original)
                    )
                    self.assertEqual(journal["round_prompt_path"], str(prompt))
                    self.assertFalse((prompt.parent / "action-0002.txt").exists())
                    for key in (
                        "revision_round",
                        "stop_fires",
                        "max_stop_fires",
                        "started_at",
                        "deadline_minutes",
                        "no_deadline",
                    ):
                        self.assertEqual(current[key], before[key])
                    with self.assertRaises(review.WorkflowError):
                        build.claim_build_action(step.ref, "action-0002")
                    self.assertEqual(self.state(), current)
                    if damage == "directory":
                        prompt.rmdir()
                    elif prompt.is_symlink():
                        prompt.unlink()
                    prompt.write_bytes(original)
                    if journal["question"]:
                        question = journal["question"]
                        build.decide_build(
                            build.BuildDecision(
                                step.ref, question["question_id"], "retry_feedback"
                            )
                        )
                    retry = build.next_build(step.ref)
                    retry_item = retry.work_items[0]
                    self.assertEqual(retry_item.action_id, "action-0002")
                    self.assertEqual(
                        Path(retry_item.prompt_path).read_bytes(), original
                    )
                    self.assertEqual(
                        self.state()["bl_workflow"]["round_prompt_path"], str(prompt)
                    )
                    done = build.execute_build_action(step.ref, retry_item.action_id)
                    self.assertEqual(done.work_items[0].kind, "reviewer")
                    self.assertEqual(provider.run.call_count, 2)

    def test_ready_retry_validates_round_source_before_writer_claim(self) -> None:
        self.request = build.BuildRequest(
            "--executor sol --seats opus task", "ready-round-drift"
        )
        provider = mock.Mock(supports_continuation=False)
        provider.is_available.return_value = (True, "")
        provider.effective_timeout.side_effect = lambda value: value
        provider.run.side_effect = lambda _text, **options: ProviderResult(
            "sol", options["model"], False, "partial", "failed", 0.0
        )
        with (
            mock.patch.object(build, "get_provider", return_value=provider),
            mock.patch.object(config, "build_resume_executor", return_value=False),
            mock.patch.object(config, "build_executor_retries", return_value=1),
        ):
            step = self.start()
            step = build.execute_build_action(step.ref, step.work_items[0].action_id)
            original = Path(self.state()["bl_workflow"]["round_prompt_path"])
            retry = step.work_items[0]
            self.assertNotEqual(Path(retry.prompt_path), original)
            content = original.read_bytes()
            original.write_bytes(content + b"Drift after retry issuance\n")
            before = self.state()
            with self.assertRaisesRegex(review.WorkflowError, "round prompt changed"):
                build.execute_build_action(step.ref, retry.action_id)
            self.assertEqual(provider.run.call_count, 1)
            self.assertEqual(self.state(), before)
            self.assertEqual(before["bl_workflow"]["action"]["status"], "ready")
            self.assertIsNone(before["bl_workflow"]["outstanding_writer"])
            original.write_bytes(content)
            launch_prompt = Path(retry.prompt_path)
            launch_prompt.write_bytes(content + b"Unverified launch payload\n")
            execute_write = execution.execute_write

            def repair_before_claim(*args: object, **kwargs: object) -> dict:
                launch_prompt.write_bytes(content)
                return execute_write(*args, **kwargs)

            with (
                mock.patch.object(
                    execution, "execute_write", side_effect=repair_before_claim
                ) as runner,
                self.assertRaisesRegex(
                    review.WorkflowError, "implementation prompt changed"
                ),
            ):
                build.execute_build_action(step.ref, retry.action_id)
            self.assertEqual(runner.call_count, 0)
            self.assertEqual(provider.run.call_count, 1)
            self.assertEqual(self.state(), before)
            launch_prompt.write_bytes(content)
            build.execute_build_action(step.ref, retry.action_id)
            self.assertEqual(provider.run.call_count, 2)

    def test_external_single_launch_without_resume(self) -> None:
        self.request = build.BuildRequest(
            "--seats opus --executor sol Write file", self.request.session_id
        )
        entered, release = threading.Event(), threading.Event()
        calls = []

        class Provider:
            supports_continuation = False

            def is_available(self) -> tuple[bool, str]:
                return True, ""

            def run(inner, prompt: str, **options: object) -> ProviderResult:
                calls.append(options)
                entered.set()
                release.wait(5)
                return ProviderResult(
                    "sol", options["model"], True, COMPLETED.decode(), None, 0.01
                )

        with (
            mock.patch.object(config, "build_resume_executor", return_value=False),
            mock.patch.object(build, "get_provider", return_value=Provider()),
        ):
            step = self.start()
            with concurrent.futures.ThreadPoolExecutor() as pool:
                running = pool.submit(
                    build.execute_build_action, step.ref, step.work_items[0].action_id
                )
                self.assertTrue(entered.wait(3))
                self.assertEqual(
                    build.execute_build_action(
                        step.ref, step.work_items[0].action_id
                    ).type,
                    "waiting",
                )
                release.set()
                completed = running.result(5)
            self.assertEqual(completed.work_items[0].kind, "reviewer")
            self.assertEqual(len(calls), 1)
            self.assertNotIn("continuation", calls[0])

    def test_revision_minor_approval_and_fresh_reviewers(self) -> None:
        step = self.panel(self.draft(), "REVISE")
        self.assertEqual(step.work_items[0].kind, "revision")
        self.assertEqual(self.state()["revision_round"], 1)
        action = step.work_items[0].action_id
        build.claim_build_action(step.ref, action)
        step = build.capture_build_return(step.ref, action, COMPLETED)
        self.assertEqual(
            self.panel(step, "REVISE", minor=True).outcome["status"], "approved"
        )

    def test_all_failed_twice_ends_review_failed(self) -> None:
        step = self.panel(self.draft(), failed=2)
        self.assertEqual(step.work_items[0].kind, "reviewer")
        self.assertEqual(self.panel(step, failed=2).outcome["status"], "review_failed")

    def test_synthesis_retry_preserves_reviewers(self) -> None:
        step = self.panel(self.draft(), synthesis_fail=True)
        self.assertEqual(step.question.kind, "synthesis_retry")
        step = self.decision(step, "retry_synthesis")
        self.assertEqual([i.kind for i in step.work_items], ["synthesis"])

    def test_native_capture_unicode_framing(self) -> None:
        step = self.start()
        item = step.work_items[0]
        build.claim_build_action(step.ref, item.action_id)
        handle = "aabcdef1234"
        out = (
            Path("/tmp").resolve()
            / f"claude-{os.getuid()}"
            / "phase5-fixture"
            / step.ref.session_segment
            / "tasks"
            / f"{handle}.output"
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        self.addCleanup(out.unlink, missing_ok=True)
        common = {
            "agentId": handle,
            "sessionId": step.ref.session_segment,
            "cwd": str(self.root),
            "version": "2.1.287",
        }
        returned = "unicode\u2028separator\nCREW_BUILD_STATUS: COMPLETED"
        records = [
            {
                **common,
                "type": "user",
                "message": {
                    "role": "user",
                    "content": native.launch_prompt(Path(item.prompt_path)),
                },
            },
            {
                **common,
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "SubagentHandback",
                            "input": {"message": returned},
                        }
                    ]
                },
            },
        ]
        out.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
        )
        build.bind_build_native(step.ref, item.action_id, handle, str(out))
        with self.assertRaises(review.WorkflowError):
            build.capture_build_native(
                step.ref, item.action_id, handle, str(out), completion_observed=False
            )
        step = build.capture_build_native(
            step.ref, item.action_id, handle, str(out), completion_observed=True
        )
        self.assertEqual(step.work_items[0].kind, "reviewer")
        self.assertEqual(
            (build._root(step.ref) / "returns" / f"{item.action_id}.txt").read_bytes(),
            returned.encode(),
        )

    def test_cleanup_retains_old_cancelled_fence_and_lock(self) -> None:
        step = self.start()
        action = step.work_items[0].action_id
        build.claim_build_action(step.ref, action)
        build.cancel_build(step.ref)
        path = loop_state.resolve("bl", step.ref.session_segment)
        old = time.time() - 9 * 86400
        os.utime(path, (old, old))
        os.utime(models.state_lock_path(path), (old, old))
        spec = importlib.util.spec_from_file_location(
            "cleanup_build_fixture",
            Path(__file__).resolve().parents[1] / "session-start.py",
        )
        hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook)
        hook.cleanup_stale_files(self.root / ".crew")
        self.assertTrue(path.exists())
        self.assertTrue(models.state_lock_path(path).exists())
        self.assertIn(
            (step.ref.session_segment, "*"),
            artifact_prune.live_run_keys(self.root / ".crew"),
        )
        build.recover_build_action(step.ref, action, "not_running")
        os.utime(path, (old, old))
        hook.cleanup_stale_files(self.root / ".crew")
        self.assertFalse(path.exists())

    def test_passive_writer_paths_do_not_import_engine(self) -> None:
        step = self.start()
        action = step.work_items[0].action_id
        build.claim_build_action(step.ref, action)
        build.cancel_build(step.ref)
        path = loop_state.resolve("bl", step.ref.session_segment)
        before = path.read_bytes()
        old = time.time() - 9 * 86400
        os.utime(path, (old, old))
        lock = models.state_lock_path(path)
        os.utime(lock, (old, old))
        inode = lock.stat().st_ino
        # A fresh interpreter guards lazy imports during actual hook execution.
        code = """
import contextlib
import importlib.abc
import io
import json
import runpy
import sys
import time
from pathlib import Path

scripts, root, session, action = sys.argv[1:]
sys.path.insert(0, scripts)
forbidden = (
    "multiagent.build_workflow", "multiagent.review_workflow",
    "multiagent.measure_twice", "multiagent.execution", "multiagent.cli",
    "multiagent.providers", "multiagent.workflow_transport",
    "multiagent.claude_native_transport", "multiagent.channels",
    "multiagent.config", "multiagent.seats",
)
attempted = []
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + ".") for name in forbidden):
            attempted.append(fullname)
            raise AssertionError("passive path imported " + fullname)
sys.meta_path.insert(0, Guard())
sys.stdin = io.StringIO(json.dumps({"session_id": "isolated-hook-reader", "cwd": root}))
output = io.StringIO()
with contextlib.redirect_stdout(output):
    try:
        hook = runpy.run_path(str(Path(scripts) / "session-start.py"), run_name="__main__")
    except SystemExit as exc:
        raise AssertionError("hook exited before boundary checks") from exc
context = json.loads(output.getvalue())["hookSpecificOutput"]["additionalContext"]
assert "--session-segment " + session in context, context
assert "--action-id " + action in context, context
assert "--confirmation not_running" in context, context
hook["cleanup_stale_files"](Path(root) / ".crew")
import artifact_prune
assert (session, "*") in artifact_prune.live_run_keys(Path(root) / ".crew")
artifact_prune.collect_prunable(Path(root) / ".crew", time.time())
assert not attempted, attempted
assert not any(name in sys.modules for name in forbidden)
print("passive writer paths verified")
"""
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                code,
                str(Path(__file__).resolve().parents[1]),
                str(self.root),
                step.ref.session_segment,
                action,
            ],
            env={
                **os.environ,
                "HOME": str(self.home),
                "CLAUDE_SESSION_ID": "isolated-hook-reader",
                "CREW_CURSOR_ENV_CAPTURE": "0",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "passive writer paths verified\n")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(lock.stat().st_ino, inode)

    def test_uncertain_writer_records_protect_cleanup_and_fail_engine_validation(
        self,
    ) -> None:
        step = self.start()
        action = step.work_items[0].action_id
        self.assertIsNone(build_state.writer_fence(self.state()))
        build.claim_build_action(step.ref, action)
        valid = self.state()
        self.assertEqual(build_state.validate_writer(valid["bl_workflow"]), action)
        self.assertEqual(build.writer_fence(valid), build_state.writer_fence(valid))
        spec = importlib.util.spec_from_file_location(
            "uncertain_writer_cleanup",
            Path(__file__).resolve().parents[1] / "session-start.py",
        )
        hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook)
        cases = []
        for key, value in (
            ("version", True),
            ("version", 2),
            ("outstanding_writer", None),
            ("outstanding_writer", False),
            ("outstanding_writer", "action-9999"),
            ("action", None),
            ("action", []),
        ):
            journal = json.loads(json.dumps(valid["bl_workflow"]))
            journal[key] = value
            cases.append(journal)
        missing = dict(valid["bl_workflow"])
        del missing["outstanding_writer"]
        cases.extend([missing, {}, [], {**valid["bl_workflow"], "unknown": 1}])
        for key, value in (
            ("status", "ready"),
            ("status", []),
            ("action_id", "wrong"),
            ("before", None),
            ("prompt_sha256", "bad"),
        ):
            journal = json.loads(json.dumps(valid["bl_workflow"]))
            journal["action"][key] = value
            cases.append(journal)
        path = loop_state.resolve("bl", step.ref.session_segment)
        lock = models.state_lock_path(path)
        old = time.time() - 9 * 86400
        for index, journal in enumerate(cases):
            with self.subTest(index=index):
                data = {**valid, "active": False, "bl_workflow": journal}
                self.assertIn("Uncertain", build_state.writer_fence(data))
                with self.assertRaises(review.WorkflowError):
                    build.journal_from_dict(journal)
                path.write_text(json.dumps(data))
                before = path.read_bytes()
                os.utime(path, (old, old))
                os.utime(lock, (old, old))
                hook.cleanup_stale_files(self.root / ".crew")
                self.assertEqual(path.read_bytes(), before)
                self.assertTrue(lock.exists())
                self.assertIn(
                    (step.ref.session_segment, "*"),
                    artifact_prune.live_run_keys(self.root / ".crew"),
                )

    def test_cleanup_serializes_orphan_lock_removal_with_new_admission(self) -> None:
        directory = self.root / ".crew"
        directory.mkdir()
        path = loop_state.resolve("bl", self.request.session_id)
        orphan = models.state_lock_path(path)
        orphan.touch()
        old = time.time() - 9 * 86400
        os.utime(orphan, (old, old))
        spec = importlib.util.spec_from_file_location(
            "cleanup_admission_fixture",
            Path(__file__).resolve().parents[1] / "session-start.py",
        )
        hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook)
        original = Path.unlink
        started = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:

            def unlink(candidate: Path, *args, **kwargs) -> None:
                if candidate == orphan:
                    future = pool.submit(self.start)
                    started.append(future)
                    with self.assertRaises(concurrent.futures.TimeoutError):
                        future.result(timeout=0.1)
                original(candidate, *args, **kwargs)

            with mock.patch.object(Path, "unlink", autospec=True, side_effect=unlink):
                hook.cleanup_stale_files(directory)
            step = started[0].result(timeout=5)
        self.assertEqual(step.type, "work_batch")
        self.assertTrue(path.exists())
        self.assertTrue(orphan.exists())
        self.assertEqual(
            build.claim_build_action(step.ref, step.work_items[0].action_id)[
                "authorization"
            ],
            "spawn",
        )

    def test_issued_commands_parse_and_cli_consumes_only_success(self) -> None:
        step = self.start()
        for name, argv in step.work_items[0].commands.items():
            if name == "native_bind" or name == "native_capture":
                argv = (*argv, "--handle", "aabcdef", "--output-file", "/tmp/path")
            cli.build_parser().parse_args(argv[1:])
        root = self.root / ".crew/requests"
        root.mkdir()
        spill = root / "request.json"
        spill.write_text(
            json.dumps({"schema": 1, "raw_arguments": self.request.raw_arguments})
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                cli.main(
                    [
                        "build",
                        "-f",
                        str(spill),
                        "--session-id",
                        self.request.session_id,
                        "--consume",
                    ]
                ),
                0,
            )
        self.assertFalse(spill.exists())

    def test_external_domain_status_and_transport_precedence(self) -> None:
        for label, text, ok, error, expected in (
            ("blocked", "CREW_BUILD_STATUS: BLOCKED", True, None, "executor_blocked"),
            ("missing", "done", True, None, "executor_report_recovery"),
            (
                "quoted",
                '"CREW_BUILD_STATUS: COMPLETED"',
                True,
                None,
                "executor_report_recovery",
            ),
            (
                "indent",
                " CREW_BUILD_STATUS: COMPLETED",
                True,
                None,
                "executor_report_recovery",
            ),
            (
                "suffix",
                "CREW_BUILD_STATUS: COMPLETED x",
                True,
                None,
                "executor_report_recovery",
            ),
            (
                "timeout",
                "CREW_BUILD_STATUS: COMPLETED",
                False,
                "timeout",
                "execution_recovery",
            ),
            (
                "failed",
                "CREW_BUILD_STATUS: COMPLETED",
                False,
                "failed",
                "execution_recovery",
            ),
        ):
            with self.subTest(label=label):
                self.request = build.BuildRequest(
                    "--executor sol --seats opus task", label
                )
                provider = mock.Mock(supports_continuation=False)
                provider.effective_timeout = lambda value: value
                provider.is_available.return_value = (True, "")
                provider.run.side_effect = lambda prompt, **opts: ProviderResult(
                    "sol",
                    opts["model"],
                    ok,
                    text,
                    error,
                    0.01,
                    transport_status=label if label in {"failed", "timeout"} else None,
                )
                with mock.patch.object(build, "get_provider", return_value=provider):
                    step = self.start()
                    step = build.execute_build_action(
                        step.ref, step.work_items[0].action_id
                    )
                self.assertEqual(step.question.kind, expected)
                self.assertEqual(self.state()["bl_workflow"]["review_generation"], 0)

    def test_native_terminated_status_and_cancelled_late_return(self) -> None:
        for status in ("failed", "timeout", "cancelled"):
            self.request = dataclasses.replace(self.request, session_id=status)
            step = self.start()
            action = step.work_items[0].action_id
            build.claim_build_action(step.ref, action)
            step = build.capture_build_return(
                step.ref, action, COMPLETED, status=status, diagnostic=status
            )
            self.assertEqual(step.question.kind, "execution_recovery")
        self.request = dataclasses.replace(self.request, session_id="cancel-race")
        step = self.start()
        action = step.work_items[0].action_id
        build.claim_build_action(step.ref, action)
        build._transaction(
            step.ref,
            lambda _data, journal: setattr(
                journal, "action", dataclasses.replace(journal.action, handle="owned")
            ),
        )
        cancels = []
        build.cancel_build(step.ref, cancel_handle=lambda h: cancels.append(h) or True)
        self.assertEqual(cancels, ["owned"])
        step = build.capture_build_return(step.ref, action, COMPLETED)
        self.assertEqual(step.type, "terminal")
        self.assertEqual(self.state()["bl_workflow"]["review_generation"], 0)
        self.assertIsNone(self.state()["bl_workflow"]["outstanding_writer"])

    def test_capture_crash_boundaries_replay_without_reclaim(self) -> None:
        for boundary in ("returns", "execution-receipts", "submissions", "state"):
            with self.subTest(boundary=boundary):
                self.request = dataclasses.replace(
                    self.request, session_id="crash-" + boundary
                )
                step = self.start()
                action = step.work_items[0].action_id
                build.claim_build_action(step.ref, action)
                original = build._write_once

                def write(path: Path, content: bytes, ref: build.BuildRef) -> None:
                    original(path, content, ref)
                    if path.parent.name == boundary:
                        raise OSError("injected crash after retained artifact")

                if boundary == "state":
                    patch = mock.patch.object(
                        build,
                        "_accept_result",
                        side_effect=OSError("injected before commit"),
                    )
                else:
                    patch = mock.patch.object(build, "_write_once", side_effect=write)
                with patch, self.assertRaises(OSError):
                    build.capture_build_return(step.ref, action, COMPLETED)
                self.assertEqual(
                    build.claim_build_action(step.ref, action)["authorization"], "wait"
                )
                replay = build.next_build(step.ref)
                self.assertEqual(
                    replay.type,
                    "waiting"
                    if boundary in {"returns", "execution-receipts"}
                    else "work_batch",
                )
                step = build.capture_build_return(step.ref, action, COMPLETED)
                self.assertEqual(step.work_items[0].kind, "reviewer")
                self.assertEqual(
                    len(self.state()["bl_workflow"]["accepted_actions"]), 1
                )
                before = self.state()
                build.capture_build_return(step.ref, action, COMPLETED)
                self.assertEqual(self.state(), before)
                with self.assertRaises(review.WorkflowError):
                    build.capture_build_return(step.ref, action, COMPLETED + b"changed")

    def test_next_reconciles_native_and_external_results_after_owner_save_failure(
        self,
    ) -> None:
        from multiagent.providers import codex

        crew_dir = self.root / ".crew"
        crew_dir.mkdir()
        (crew_dir / "config.toml").write_text(
            "[build]\nresume_executor = false\nexecutor_retries = 1\n"
        )
        config._reset_cache_for_tests()
        for external, status, terminal in (
            (e, s, t)
            for e in (False, True)
            for s in ("completed", "blocked", "failed")
            for t in ("active", "cancelled", "bound", "bounded", "done")
        ):
            with self.subTest(external=external, status=status, terminal=terminal):
                self.request = build.BuildRequest(
                    f"--executor {'sol' if external else 'crew:executor'} --seats opus task",
                    f"save-failure-{external}-{status}-{terminal}",
                )
                report = (
                    b"CREW_BUILD_STATUS: BLOCKED" if status == "blocked" else COMPLETED
                )
                provider = build.get_provider("sol")
                original = models.atomic_write_json

                def save(path: Path, value: dict) -> None:
                    if value.get("bl_workflow", {}).get("accepted_actions"):
                        raise OSError(
                            "injected owner save failure after durable result"
                        )
                    original(path, value)

                def launch(
                    argv: list[str], **_kwargs: object
                ) -> tuple[int, bytes, bytes]:
                    terminate_owner()
                    Path(argv[argv.index("-o") + 1]).write_bytes(report)
                    return (
                        1 if status == "failed" else 0,
                        b"",
                        b"injected provider failure",
                    )

                def terminate_owner() -> None:
                    if terminal == "cancelled":
                        build.cancel_build(step.ref, reason="cancel during execution")
                    elif terminal in {"bound", "bounded", "done"}:
                        loop_state.mutate(
                            loop_state.resolve("bl", step.ref.session_segment),
                            lambda data: dict(
                                data,
                                **(
                                    {"stop_fires": 10000}
                                    if terminal in {"bound", "bounded"}
                                    else {"phase": "done"}
                                ),
                            ),
                        )
                        if terminal == "bounded":
                            self.assertEqual(
                                build.next_build(step.ref).type, "terminal"
                            )

                with (
                    mock.patch.object(build, "get_provider", return_value=provider)
                    if external
                    else contextlib.nullcontext(),
                    mock.patch.object(execution, "get_provider", return_value=provider)
                    if external
                    else contextlib.nullcontext(),
                    mock.patch.object(
                        provider, "is_available", return_value=(True, "")
                    ),
                    mock.patch.object(
                        codex, "run_reaped", side_effect=launch
                    ) as runner,
                ):
                    step = self.start()
                    action_id = step.work_items[0].action_id
                    if not external:
                        build.claim_build_action(step.ref, action_id)
                        handle = "aabcdef1234"
                        out = (
                            Path("/tmp").resolve()
                            / f"claude-{os.getuid()}"
                            / "revision4-fixture"
                            / step.ref.session_segment
                            / "tasks"
                            / f"{handle}.output"
                        )
                        out.parent.mkdir(parents=True, exist_ok=True)
                        self.addCleanup(out.unlink, missing_ok=True)
                        common = {
                            "agentId": handle,
                            "sessionId": step.ref.session_segment,
                            "cwd": str(self.root),
                            "version": native.SUPPORTED_VERSION,
                        }
                        records = [
                            {
                                **common,
                                "type": "user",
                                "message": {
                                    "role": "user",
                                    "content": native.launch_prompt(
                                        Path(step.work_items[0].prompt_path)
                                    ),
                                },
                            },
                            {
                                **common,
                                "type": "assistant",
                                "message": {
                                    "content": [
                                        {
                                            "type": "tool_use",
                                            "name": "SubagentHandback",
                                            "input": {"message": report.decode()},
                                        }
                                    ]
                                },
                            },
                        ]
                        out.write_text("".join(json.dumps(r) + "\n" for r in records))
                        build.bind_build_native(step.ref, action_id, handle, str(out))
                        terminate_owner()
                    with (
                        mock.patch.object(
                            models, "atomic_write_json", side_effect=save
                        ),
                        self.assertRaisesRegex(OSError, "owner save failure"),
                    ):
                        if external:
                            build.execute_build_action(step.ref, action_id)
                        else:
                            native.capture_native_return(
                                native.NativeLaunch(step.ref, action_id, handle, out),
                                completion_observed=True,
                                status="failed" if status == "failed" else "ok",
                            )
                    state = self.state()["bl_workflow"]
                    self.assertEqual(state["outstanding_writer"], action_id)
                    self.assertFalse(state["accepted_actions"])
                    terminal_before = self.state()
                    artifacts = {
                        str(p): p.read_bytes()
                        for p in build._root(step.ref).rglob("*")
                        if p.is_file()
                    }
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        self.assertEqual(
                            cli.main(
                                [
                                    "build-next",
                                    "--session-segment",
                                    step.ref.session_segment,
                                    "--loop-instance-id",
                                    step.ref.loop_instance_id,
                                ]
                            ),
                            0,
                        )
                    replay = json.loads(output.getvalue())
                    self.assertEqual(replay["schema"], 1)
                    state = self.state()["bl_workflow"]
                    self.assertIsNone(state["outstanding_writer"])
                    self.assertEqual(
                        [r["status"] for r in state["accepted_actions"]], [status]
                    )
                    self.assertEqual(
                        state["round_attempt"],
                        2
                        if terminal == "active" and external and status == "failed"
                        else 1,
                    )
                    before = self.state()
                    build.next_build(step.ref)
                    self.assertEqual(self.state(), before)
                    self.assertEqual(runner.call_count, int(external))
                    self.assertTrue(
                        all(
                            Path(path).read_bytes() == content
                            for path, content in artifacts.items()
                        )
                    )
                    if terminal != "active":
                        self.assertEqual(replay["type"], "terminal")
                        self.assertFalse(before["active"])
                        self.assertEqual(state["review_generation"], 0)
                        self.assertIsNone(state["review_ref"])
                        self.assertIsNone(state["question"])
                        for field in (
                            "stop_fires",
                            "max_stop_fires",
                            "deadline_minutes",
                            "started_at",
                            "revision_round",
                            "awaiting_input",
                        ):
                            self.assertEqual(before[field], terminal_before[field])
                        if terminal in {"cancelled", "bounded"}:
                            for field in (
                                "reason",
                                "exit_kind",
                                "force_exit",
                                "completed_at",
                            ):
                                self.assertEqual(
                                    before.get(field), terminal_before.get(field)
                                )
                                self.assertEqual(
                                    field in before, field in terminal_before
                                )
                        if terminal in {"bound", "bounded"}:
                            self.assertTrue(before["force_exit"])
                            self.assertEqual(before["exit_kind"], "force_exit")
                            self.assertIn("livelock circuit breaker", before["reason"])
                        resumed = build.resume_build(step.ref.session_segment)
                        self.assertEqual(resumed.type, "terminal")
                        self.assertEqual(self.state(), before)
                    elif status == "blocked" or status == "failed" and not external:
                        self.assertEqual(replay["type"], "needs_input")
                    else:
                        self.assertEqual(
                            replay["work_items"][0]["kind"],
                            "implementation" if status == "failed" else "reviewer",
                        )
                build.cancel_build(step.ref)

    def test_inactive_replay_preserves_uncertain_or_invalid_completion(self) -> None:
        for case in ("missing", "corrupt", "foreign", "unconfirmed"):
            with self.subTest(case=case):
                self.request = dataclasses.replace(
                    self.request, session_id="inactive-" + case
                )
                step = self.start()
                action_id = step.work_items[0].action_id
                build.claim_build_action(step.ref, action_id)
                build.cancel_build(step.ref, "cancelled with uncertain writer")
                if case in {"corrupt", "foreign"}:
                    with (
                        mock.patch.object(
                            build, "_accept_result", side_effect=OSError("save crash")
                        ),
                        self.assertRaises(OSError),
                    ):
                        build.capture_build_return(step.ref, action_id, COMPLETED)
                    path = build._root(step.ref) / "submissions" / f"{action_id}.json"
                    if case == "corrupt":
                        path.write_bytes(b"{")
                    else:
                        value = json.loads(path.read_bytes())
                        value["action_id"] = "action-9999"
                        path.write_text(json.dumps(value))
                elif case == "unconfirmed":
                    with self.assertRaises(review.WorkflowError):
                        build.capture_build_return(
                            step.ref,
                            action_id,
                            COMPLETED,
                            status="termination_unconfirmed",
                        )
                before = self.state()
                for call in (
                    lambda: build.next_build(step.ref),
                    lambda: build.resume_build(step.ref.session_segment),
                ):
                    replay = call()
                    self.assertEqual(replay.type, "terminal")
                    if case in {"corrupt", "foreign"}:
                        self.assertIn("retained completion rejected", replay.display)
                    self.assertEqual(self.state(), before)
                    self.assertEqual(
                        self.state()["bl_workflow"]["outstanding_writer"], action_id
                    )
                    self.assertFalse(self.state()["bl_workflow"]["accepted_actions"])

    def test_retained_completion_validation_preserves_uncertain_writer(self) -> None:
        cases = (
            "missing_receipt",
            "missing_return",
            "broken_submission",
            "foreign_submission",
            "wrong_action",
            "broken_receipt",
            "missing_before",
            "bad_after",
            "foreign_receipt",
            "wrong_route",
            "wrong_route_type",
            "wrong_before",
            "wrong_content_hash",
            "wrong_receipt_hash",
            "wrong_status",
            "wrong_content",
        )
        for case in cases:
            with self.subTest(case=case):
                self.request = dataclasses.replace(
                    self.request, session_id="retained-" + case
                )
                step = self.start()
                action_id = step.work_items[0].action_id
                build.claim_build_action(step.ref, action_id)
                with (
                    mock.patch.object(
                        build, "_accept_result", side_effect=OSError("injected crash")
                    ),
                    self.assertRaises(OSError),
                ):
                    build.capture_build_return(step.ref, action_id, COMPLETED)
                root = build._root(step.ref)
                submission = root / "submissions" / f"{action_id}.json"
                receipt = root / "execution-receipts" / f"{action_id}.json"
                returned = root / "returns" / f"{action_id}.txt"
                result = json.loads(submission.read_bytes())
                facts = json.loads(receipt.read_bytes())
                if case == "missing_receipt":
                    receipt.unlink()
                elif case == "missing_return":
                    returned.unlink()
                elif case == "broken_submission":
                    submission.write_bytes(b"{")
                elif case == "foreign_submission":
                    result["ref"]["loop_instance_id"] = "foreign"
                elif case == "wrong_action":
                    result["action_id"] = "action-9999"
                elif case == "broken_receipt":
                    receipt.write_bytes(b"{}")
                elif case == "missing_before":
                    del facts["before"]
                elif case == "bad_after":
                    facts["after"] = {"head": []}
                elif case == "foreign_receipt":
                    facts["ref"]["loop_instance_id"] = "foreign"
                elif case == "wrong_route":
                    facts["executor"]["executor"] = "sol"
                elif case == "wrong_route_type":
                    facts["executor"]["resume_executor"] = int(
                        facts["executor"]["resume_executor"]
                    )
                elif case == "wrong_before":
                    facts["before"]["branch"] = "foreign"
                elif case == "wrong_content_hash":
                    facts["returned_sha256"] = "0" * 64
                elif case == "wrong_receipt_hash":
                    result["execution_receipt_sha256"] = "0" * 64
                elif case == "wrong_status":
                    result["status"] = "blocked"
                elif case == "wrong_content":
                    returned.write_bytes(COMPLETED + b"tampered")
                if case in {
                    "missing_before",
                    "bad_after",
                    "foreign_receipt",
                    "wrong_route",
                    "wrong_route_type",
                    "wrong_before",
                    "wrong_content_hash",
                }:
                    receipt.write_bytes(build.canonical(facts))
                    result["execution_receipt_sha256"] = build.sha256(
                        receipt.read_bytes()
                    )
                if case != "broken_submission":
                    submission.write_bytes(build.canonical(result))
                state_before = self.state()
                artifacts = {
                    str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()
                }
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(
                        cli.main(
                            [
                                "build-next",
                                "--session-segment",
                                step.ref.session_segment,
                                "--loop-instance-id",
                                step.ref.loop_instance_id,
                            ]
                        ),
                        2,
                    )
                error = json.loads(output.getvalue())
                self.assertEqual(error["schema"], 1)
                self.assertIn(error["code"], {"invalid_result", "invalid_build_record"})
                self.assertEqual(self.state(), state_before)
                self.assertTrue(
                    all(
                        Path(path).read_bytes() == content
                        for path, content in artifacts.items()
                    )
                )
                # No uncertain artifacts are used to authorize a replacement.
                build.cancel_build(step.ref)
                with self.assertRaisesRegex(
                    loop_state.LoopStateError, "Outstanding writer"
                ):
                    self.start()
                submission.unlink()
                build.recover_build_action(step.ref, action_id, "not_running")

    def test_unknown_workspace_parks_native_and_chained_execution(self) -> None:
        from multiagent.providers import cursor

        for external, case in (
            (e, c)
            for e in (False, True)
            for c in ("missing_metadata", "corrupt_index", "unmerged_index")
        ):
            with self.subTest(external=external, case=case):
                self.request = build.BuildRequest(
                    f"--executor {'cursor-auto' if external else 'crew:executor'} --seats opus task",
                    f"unknown-{external}-{case}",
                )
                provider = build.get_provider("cursor-auto")
                with (
                    mock.patch.object(build, "get_provider", return_value=provider)
                    if external
                    else contextlib.nullcontext(),
                    mock.patch.object(execution, "get_provider", return_value=provider)
                    if external
                    else contextlib.nullcontext(),
                    mock.patch.object(
                        provider, "is_available", return_value=(True, "")
                    ),
                    mock.patch.object(
                        provider, "_supports_continuation_runtime", return_value=True
                    ),
                    mock.patch.object(
                        provider, "_supports_stream_json_runtime", return_value=True
                    ),
                    mock.patch.object(cursor, "run_reaped") as runner,
                ):
                    step = self.start()
                    action = step.work_items[0].action_id
                    metadata = self.root / ".git"
                    index = metadata / "index"
                    original_index = index.read_bytes() if index.exists() else None
                    backup = self.home / "git-backup"
                    try:
                        if case == "missing_metadata":
                            metadata.rename(backup)
                        elif case == "corrupt_index":
                            index.write_bytes(b"corrupt index")
                        else:
                            blob = (
                                subprocess.check_output(
                                    ["git", "hash-object", "-w", "--stdin"],
                                    input=b"conflict\n",
                                    cwd=self.root,
                                )
                                .decode()
                                .strip()
                            )
                            subprocess.run(
                                ["git", "update-index", "--index-info"],
                                input="".join(
                                    f"100644 {blob} {stage}\tconflict.txt\n"
                                    for stage in (1, 2, 3)
                                ),
                                text=True,
                                cwd=self.root,
                                check=True,
                            )
                        self.assertFalse(
                            execution.observe_workspace(str(self.root)).complete
                        )
                        argv = [
                            "build-execute" if external else "build-claim",
                            "--session-segment",
                            step.ref.session_segment,
                            "--loop-instance-id",
                            step.ref.loop_instance_id,
                            "--action-id",
                            action,
                        ]
                        with contextlib.redirect_stdout(io.StringIO()) as output:
                            self.assertEqual(cli.main(argv), 0)
                        projected = json.loads(output.getvalue())
                        parked = projected if external else projected["step"]
                        self.assertEqual(parked["type"], "needs_input")
                        self.assertEqual(parked["question"]["kind"], "workspace_guard")
                        self.assertEqual(
                            parked["question"]["workspace_sha256"],
                            build._workspace_digest(),
                        )
                        self.assertTrue(self.state()["awaiting_input"])
                        self.assertIsNone(
                            self.state()["bl_workflow"]["outstanding_writer"]
                        )
                        self.assertEqual(
                            build.next_build(step.ref).question.question_id,
                            parked["question"]["question_id"],
                        )
                        runner.assert_not_called()
                        decision = build.next_build(step.ref)
                        with self.assertRaisesRegex(
                            review.WorkflowError, "cannot waive"
                        ):
                            self.decision(decision, "recheck_workspace")
                    finally:
                        if backup.exists():
                            backup.rename(metadata)
                        if original_index is None:
                            index.unlink(missing_ok=True)
                        else:
                            index.write_bytes(original_index)
                    restored = build.next_build(step.ref)
                    self.assertEqual(
                        self.decision(restored, "recheck_workspace").type, "work_batch"
                    )
                build.cancel_build(step.ref)

    def test_wrong_receipts_and_external_host_capture_refuse(self) -> None:
        step = self.start()
        action = step.work_items[0].action_id
        build.claim_build_action(step.ref, action)
        with (
            mock.patch.object(build, "_accept_result", side_effect=OSError("crash")),
            self.assertRaises(OSError),
        ):
            build.capture_build_return(step.ref, action, COMPLETED)
        result = build.parse_build_result(
            json.loads(
                (
                    build._root(step.ref) / "submissions" / (action + ".json")
                ).read_bytes()
            )
        )
        with self.assertRaises(review.WorkflowError):
            build.submit_build_action(
                dataclasses.replace(result, execution_receipt_sha256="0" * 64)
            )
        self.assertEqual(self.state()["bl_workflow"]["review_generation"], 0)
        self.assertEqual(build.cancel_build(step.ref).type, "terminal")
        self.assertIsNone(self.state()["bl_workflow"]["outstanding_writer"])
        self.assertEqual(len(self.state()["bl_workflow"]["accepted_actions"]), 1)
        self.request = dataclasses.replace(
            self.request, raw_arguments="--executor sol --seats opus task"
        )
        step = self.start()
        with self.assertRaises(review.WorkflowError):
            build.capture_build_return(
                step.ref, step.work_items[0].action_id, COMPLETED
            )

    def test_pause_bound_cancel_and_wrong_owner_claim(self) -> None:
        step = self.start()
        action = step.work_items[0].action_id
        before = self.state()
        with self.assertRaises(review.WorkflowError):
            build.claim_build_action(
                dataclasses.replace(step.ref, loop_instance_id="foreign"), action
            )
        self.assertEqual(self.state(), before)
        loop_state.mutate(
            loop_state.resolve("bl", self.request.session_id),
            lambda data: dict(data, awaiting_input=True),
        )
        with self.assertRaises(review.WorkflowError):
            build.claim_build_action(step.ref, action)
        loop_state.mutate(
            loop_state.resolve("bl", self.request.session_id),
            lambda data: dict(data, awaiting_input=False, stop_fires=10000),
        )
        with self.assertRaises(review.WorkflowError):
            build.claim_build_action(step.ref, action)
        self.assertEqual(build.cancel_build(step.ref).type, "terminal")

    def test_capped_stop_next_restores_unanswered_question_parking(self) -> None:
        step = self.draft(b"Need human guidance\nCREW_BUILD_STATUS: BLOCKED")
        path = loop_state.resolve("bl", self.request.session_id)
        loop_state.mutate(
            path, lambda data: dict(data, parked_fires=data["max_parked_fires"])
        )
        script = Path(__file__).resolve().parents[1] / "persistent-mode.py"

        def stop() -> dict:
            result = subprocess.run(
                [sys.executable, str(script)],
                input=json.dumps(
                    {"session_id": self.request.session_id, "cwd": str(self.root)}
                ),
                capture_output=True,
                text=True,
                env={**os.environ, "HOME": str(self.home)},
                check=True,
            )
            return json.loads(result.stdout)

        capped = stop()
        self.assertEqual(capped["decision"], "block")
        self.assertFalse(self.state()["awaiting_input"])
        stop_fires = self.state()["stop_fires"]
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(
                cli.main(
                    [
                        "build-next",
                        "--session-segment",
                        step.ref.session_segment,
                        "--loop-instance-id",
                        step.ref.loop_instance_id,
                    ]
                ),
                0,
            )
        replay = json.loads(output.getvalue())
        self.assertEqual(replay["type"], "needs_input")
        self.assertEqual(replay["question"]["question_id"], step.question.question_id)
        self.assertTrue(self.state()["awaiting_input"])
        self.assertNotEqual(stop().get("decision"), "block")
        self.assertEqual(self.state()["stop_fires"], stop_fires)
        self.assertEqual(self.state()["parked_fires"], 1)

    def test_large_executor_report_is_verified_reference_in_reviewer_retries(
        self,
    ) -> None:
        from multiagent.providers import agy, cursor

        directory = self.root / ".crew"
        directory.mkdir()
        (directory / "config.toml").write_text(
            '[seats.fixture-agy]\nmodel = "fixture"\nvia = ["agy"]\n'
            '[seats.fixture-cursor]\nmodel = "fixture"\nvia = ["cursor"]\n'
        )
        config._reset_cache_for_tests()
        report = b"EXECUTOR-EVIDENCE-LONG-BODY\n" * 15000 + COMPLETED
        self.assertGreater(len(report), 340000)
        self.request = build.BuildRequest(
            "--executor crew:executor --seats fixture-cursor,fixture-agy,opus task",
            "large-executor-report",
        )
        step = self.draft(report)
        report_path = build._root(step.ref) / "returns" / "action-0001.txt"
        retained = {
            p: p.read_bytes()
            for p in report_path.parent.parent.rglob("*.json")
            if p.parent.name
            in {"transport-receipts", "execution-receipts", "submissions"}
        }
        report_digest = build.sha256(report)
        issued = step.work_items[0].review_ref
        run = review._guard_review_path(
            session_segment=issued.session_segment, run_id=issued.run_id, create=False
        )
        record = json.loads((run / "run.json").read_bytes())
        self.assertEqual(
            record["workflow_identity"]["executor_report"],
            {"path": str(report_path), "sha256": report_digest},
        )
        self.assertLess(len(json.dumps(record)), 16000)
        prompts = []
        for attempt in (1, 2):
            for item in step.work_items:
                prompt = Path(item.prompt_path).read_bytes()
                prompts.append(prompt)
                self.assertLess(len(prompt), 16000)
                self.assertIn(str(report_path).encode(), prompt)
                self.assertIn(report_digest.encode(), prompt)
                self.assertIn(b"Read the full retained executor report", prompt)
                self.assertNotIn(b"EXECUTOR-EVIDENCE-LONG-BODY", prompt)
                if item.driver != "external":
                    review.claim_review_action(
                        review.ClaimRequest(item.review_ref, item.action_id)
                    )
                    transport.capture_review_return(
                        item.review_ref, item.action_id, VALID
                    )
                    continue
                provider = build.get_provider(item.review_item.seat)
                module = cursor if item.review_item.channel == "cursor" else agy

                def launch(
                    argv: list[str], **_kwargs: object
                ) -> tuple[int, bytes, bytes]:
                    # Actual adapters have checked their prompt cap before here.
                    self.assertTrue(any(str(report_path) in word for word in argv))
                    return (
                        (1, b"", b"fixture outage") if attempt == 1 else (0, VALID, b"")
                    )

                with (
                    mock.patch.object(
                        review, "_frozen_external_provider", return_value=provider
                    ),
                    mock.patch.object(
                        provider, "is_available", return_value=(True, "")
                    ),
                    mock.patch.object(
                        module, "run_reaped", side_effect=launch
                    ) as runner,
                ):
                    review.execute_external_review(item.review_ref, item.action_id)
                self.assertEqual(runner.call_count, 1)
            step = build.next_build(step.ref)
            if attempt == 1:
                item = step.work_items[0]
                self.assertEqual(item.kind, "synthesis")
                review.claim_review_action(
                    review.ClaimRequest(item.review_ref, item.action_id)
                )
                transport.capture_review_return(
                    item.review_ref,
                    item.action_id,
                    b"No usable reviews\nBLOCKING_CAUSES: 0\n",
                    judgment=transport.SynthesisJudgment("APPROVED", False),
                )
                step = build.next_build(step.ref)
                self.assertEqual(step.question.kind, "completion_advisory")
                step = self.decision(step, "retry_review")
        self.assertEqual(len(prompts), 5)
        self.assertEqual(step.work_items[0].kind, "synthesis")
        self.assertEqual(report_path.read_bytes(), report)
        self.assertTrue(
            all(path.read_bytes() == content for path, content in retained.items())
        )
        # No retry can quietly substitute context if the retained report changes.
        report_path.write_bytes(b"tampered")
        with self.assertRaisesRegex(review.WorkflowError, "report changed"):
            review.next_review(step.work_items[0].review_ref)
        report_path.write_bytes(report)
        self.assertEqual(build.next_build(step.ref).work_items[0].kind, "synthesis")

    def test_blocked_and_invalid_steps_expose_report_and_bound_choices(self) -> None:
        from multiagent.providers import codex

        directory = self.root / ".crew"
        directory.mkdir()
        (directory / "config.toml").write_text("[build]\nresume_executor = false\n")
        config._reset_cache_for_tests()
        for external in (False, True):
            for status, marker in (
                ("blocked", b"CREW_BUILD_STATUS: BLOCKED\n"),
                ("invalid_report", b"CREW_BUILD_STATUS: COMPLETED \n"),
                ("invalid_report", b"invalid utf8: \xff"),
            ):
                with self.subTest(external=external, status=status, marker=marker):
                    self.request = build.BuildRequest(
                        f"--executor {'sol' if external else 'crew:executor'} --seats opus task",
                        f"report-choice-{external}-{status}-{len(marker)}",
                    )
                    report = (
                        b"Real blocker: provide the missing acceptance input.\n" * 8000
                        + marker
                    )
                    provider = build.get_provider("sol")
                    with (
                        mock.patch.object(build, "get_provider", return_value=provider),
                        mock.patch.object(
                            provider, "is_available", return_value=(True, "")
                        ),
                        mock.patch.object(
                            codex, "run_reaped", return_value=(0, report, b"")
                        ),
                    ):
                        step = self.start()
                        action_id = step.work_items[0].action_id
                        if external:
                            step = build.execute_build_action(step.ref, action_id)
                        else:
                            build.claim_build_action(step.ref, action_id)
                            step = build.capture_build_return(
                                step.ref, action_id, report
                            )
                    self.assertEqual(step.type, "needs_input")
                    path = build._root(step.ref) / "returns" / f"{action_id}.txt"
                    self.assertEqual(path.read_bytes(), report)
                    self.assertIn(str(path), step.question.text)
                    self.assertIn(build.sha256(report), step.question.text)
                    for choice in (
                        "answer_executor",
                        "retry_executor",
                        "stack_edits",
                        "cancel",
                    ):
                        self.assertIn(choice, step.question.text)
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        self.assertEqual(
                            cli.main(
                                list(build._owner_argv(step.ref, "build-next"))[1:]
                            ),
                            0,
                        )
                    projected = json.loads(output.getvalue())
                    self.assertIn(str(path), projected["question"]["text"])
                    self.assertLess(len(output.getvalue()), 16000)
                    step = self.decision(
                        step,
                        "answer_executor",
                        answer="Use the supplied acceptance input",
                        confirmation="stack_edits",
                        workspace_sha256=step.question.workspace_sha256,
                    )
                    if marker == b"invalid utf8: \xff":
                        self.assertEqual(step.type, "work_batch")
                        feedback = Path(
                            self.state()["bl_workflow"]["feedback_paths"][-1]
                        )
                        self.assertNotEqual(feedback, path)
                        self.assertIn(
                            "INVALID UTF-8 EXECUTOR REPORT", feedback.read_text()
                        )
                        self.assertEqual(path.read_bytes(), report)
                    else:
                        self.assertEqual(step.type, "work_batch")
                        prompt = Path(step.work_items[0].prompt_path).read_text()
                        self.assertIn("Use the supplied acceptance input", prompt)
                        self.assertIn(build.sha256(report), prompt)
                        path.write_bytes(b"changed retained executor context")
                        with (
                            mock.patch.object(
                                build, "get_provider", return_value=provider
                            ),
                            mock.patch.object(
                                provider, "is_available", return_value=(True, "")
                            ),
                            mock.patch.object(codex, "run_reaped") as runner,
                            self.assertRaisesRegex(
                                review.WorkflowError, "report changed"
                            ),
                        ):
                            if external:
                                build.execute_build_action(
                                    step.ref, step.work_items[0].action_id
                                )
                            else:
                                build.claim_build_action(
                                    step.ref, step.work_items[0].action_id
                                )
                        runner.assert_not_called()
                        self.assertIsNone(
                            self.state()["bl_workflow"]["outstanding_writer"]
                        )
                        path.write_bytes(report)
                    build.cancel_build(step.ref)

    def test_native_build_runtime_refuses_every_tampered_outer_projection(self) -> None:
        class Runtime:
            def __init__(self) -> None:
                self.launched = []

            def launch(self, item: build.BuildWorkItem) -> str:
                self.launched.append(item)
                return item.action_id

            def completions(self, handles: tuple[str, ...]):
                return ()

        changes = {
            "role": "crew:executor",
            "model": "tampered-model",
            "access": "workspace-write",
            "prompt_path": str(self.root / "foreign.txt"),
            "returned_path": str(self.root / "foreign-return.txt"),
            "kind": "implementation",
            "channel": "codex",
            "timeout_seconds": 1,
            "host_allowance_seconds": 1,
            "commands": {"claim": ("tampered",)},
            "review_ref": review.ReviewRef(
                "foreign", "run-deadbeefdead", "attempt-0001", "0" * 64
            ),
        }
        for key, value in changes.items():
            with self.subTest(field=key):
                self.request = dataclasses.replace(
                    self.request, session_id="tampered-" + key
                )
                step = self.draft()
                item = dataclasses.replace(step.work_items[0], **{key: value})
                runtime = Runtime()
                with self.assertRaises(review.WorkflowError):
                    transport.run_build_batch(
                        dataclasses.replace(step, work_items=(item,)), runtime
                    )
                self.assertFalse(runtime.launched)
                build.cancel_build(step.ref)
        # A foreign owner is also rejected before any claim/launch.
        self.request = dataclasses.replace(self.request, session_id="tampered-owner")
        step = self.draft()
        runtime = Runtime()
        with self.assertRaises(review.WorkflowError):
            transport.run_build_batch(
                dataclasses.replace(
                    step,
                    work_items=(
                        dataclasses.replace(
                            step.work_items[0],
                            owner=build.BuildRef("foreign", "foreign-owner"),
                        ),
                    ),
                ),
                runtime,
            )
        self.assertFalse(runtime.launched)
        build.cancel_build(step.ref)
        # Even a valid claim from another current build cannot use this wrapper's owner.
        self.request = dataclasses.replace(
            self.request, session_id="foreign-valid-review"
        )
        foreign = self.draft()
        self.request = dataclasses.replace(
            self.request, session_id="target-valid-review"
        )
        target = self.draft()
        forged = dataclasses.replace(
            foreign.work_items[0],
            owner=target.ref,
            commands={
                **foreign.work_items[0].commands,
                "next": build._owner_argv(target.ref, "build-next"),
            },
        )
        runtime = Runtime()
        with self.assertRaises(review.WorkflowError):
            transport.run_build_batch(
                dataclasses.replace(target, work_items=(forged,)), runtime
            )
        self.assertFalse(runtime.launched)
        build.cancel_build(foreign.ref)
        build.cancel_build(target.ref)
        self.request = dataclasses.replace(
            self.request, session_id="authoritative-launch"
        )
        step = self.draft()
        runtime = Runtime()
        transport.run_build_batch(step, runtime)
        self.assertEqual(runtime.launched, list(step.work_items))
        self.assertTrue(
            all(
                item.role in {"crew:reviewer"} and item.access != "workspace-write"
                for item in runtime.launched
            )
        )

    def test_executor_probes_cannot_mutate_or_launch_a_new_action(self) -> None:
        directory = self.root / ".crew"
        directory.mkdir()
        (directory / "config.toml").write_text(
            "[build]\nexecutor_retries = 1\nresume_executor = false\n"
        )
        config._reset_cache_for_tests()
        for drift in (False, True):
            with self.subTest(drift=drift):
                self.request = build.BuildRequest(
                    "--executor sol --seats opus task", f"executor-probe-{drift}"
                )
                with mock.patch.object(config, "dispatch_timeout", return_value=100):
                    step = self.start()
                observed, release = threading.Event(), threading.Event()
                stale, current = mock.Mock(), mock.Mock()
                for provider in (stale, current):
                    provider.is_available.return_value = (True, "")

                def floor(base: int) -> int:
                    self.assertEqual(base, 100)
                    observed.set()
                    self.assertTrue(release.wait(5))
                    return base + int(drift)

                stale.effective_timeout.side_effect = floor
                current.effective_timeout.side_effect = lambda base: base
                current.run.return_value = ProviderResult(
                    "sol",
                    self.state()["bl_workflow"]["executor"]["model"],
                    False,
                    "",
                    "fixture failure",
                    0.0,
                    transport_status="failed",
                )
                with (
                    mock.patch.object(
                        build,
                        "get_provider",
                        side_effect=lambda _seat: (
                            stale
                            if threading.current_thread().name.startswith(
                                "stale-executor"
                            )
                            else current
                        ),
                    ),
                    concurrent.futures.ThreadPoolExecutor(
                        thread_name_prefix="stale-executor"
                    ) as pool,
                ):
                    delayed = pool.submit(
                        build.execute_build_action,
                        step.ref,
                        step.work_items[0].action_id,
                    )
                    self.assertTrue(observed.wait(3))
                    try:
                        newer = build.execute_build_action(
                            step.ref, step.work_items[0].action_id
                        )
                        self.assertEqual(newer.work_items[0].action_id, "action-0002")

                        def update_policy(
                            _data: dict, journal: build.BuildJournal
                        ) -> None:
                            journal.policy = dataclasses.replace(
                                journal.policy, timeout_seconds=101
                            )

                        if drift:
                            build._transaction(step.ref, update_policy)
                        before = self.state()
                    finally:
                        release.set()
                    result = delayed.result(5)
                stale.run.assert_not_called()
                self.assertEqual(current.run.call_count, 1)
                self.assertEqual(result.work_items[0].action_id, "action-0002")
                self.assertEqual(self.state(), before)
                self.assertIsNone(self.state()["bl_workflow"]["question"])
                build.cancel_build(step.ref)

    def test_frozen_base_timeout_detects_executor_and_review_floor_decreases(
        self,
    ) -> None:
        directory = self.root / ".crew"
        directory.mkdir()
        (directory / "config.toml").write_text("[build]\nresume_executor = false\n")
        config._reset_cache_for_tests()
        provider = mock.Mock()
        provider.is_available.return_value = (True, "")
        provider.effective_timeout.side_effect = lambda base: max(base, 300)
        self.request = build.BuildRequest(
            "--executor sol --seats opus task", "executor-floor-drop"
        )
        with (
            mock.patch.object(build, "get_provider", return_value=provider),
            mock.patch.object(config, "dispatch_timeout", return_value=100),
        ):
            step = self.start()
            self.assertEqual(step.work_items[0].timeout_seconds, 300)
            self.assertEqual(
                self.state()["bl_workflow"]["policy"]["base_timeout_seconds"], 100
            )
            provider.effective_timeout.side_effect = lambda base: max(base, 200)
            parked = build.execute_build_action(step.ref, step.work_items[0].action_id)
        self.assertEqual(parked.question.kind, "route_unavailable")
        provider.run.assert_not_called()
        provider.effective_timeout.assert_called_with(100)
        build.cancel_build(step.ref)
        self.request = build.BuildRequest(
            "--executor crew:executor --seats sol task", "review-floor-drop"
        )
        with (
            mock.patch.object(review.config, "default_timeout", return_value=100),
            mock.patch.object(review, "_provider_timeout", return_value=300),
        ):
            step = self.draft()
        item = step.work_items[0]
        with (
            mock.patch.object(
                review, "_frozen_external_provider", return_value=provider
            ),
            self.assertRaisesRegex(review.WorkflowError, "fresh review decision"),
        ):
            review.execute_external_review(item.review_ref, item.action_id)
        provider.effective_timeout.assert_called_with(100)
        provider.run.assert_not_called()
        self.assertEqual(
            build.next_build(step.ref).question.kind, "review_timeout_changed"
        )

    def test_group_signals_precede_reap_and_unsupported_observation_stays_uncertain(
        self,
    ) -> None:
        import signal

        from multiagent.providers import _proc

        real_popen, real_killpg = subprocess.Popen, os.killpg
        events = []

        def launch(*args: object, **kwargs: object) -> subprocess.Popen:
            proc = real_popen(*args, **kwargs)
            real_wait = proc.wait

            def reap(*args: object, **kwargs: object) -> int:
                events.append(("reap", proc.pid))
                return real_wait(*args, **kwargs)

            proc.wait = reap
            return proc

        def signal_group(pgid: int, sig: int) -> None:
            events.append(("probe" if sig == 0 else "signal", pgid))
            if sig != 0:
                self.assertNotIn(("reap", pgid), events)
            real_killpg(pgid, sig)

        with (
            mock.patch.object(_proc.subprocess, "Popen", side_effect=launch),
            mock.patch.object(_proc.os, "killpg", side_effect=signal_group),
        ):
            result = _proc.run_reaped(
                [
                    sys.executable,
                    "-c",
                    "import sys;sys.stdout.buffer.write(b'exact\\r\\n');sys.exit(1)",
                ],
                timeout=2,
                capture_bytes=True,
            )
        self.assertEqual(result, (1, b"exact\r\n", b""))
        self.assertTrue(any(kind == "signal" for kind, _pid in events))
        self.assertTrue(any(kind == "reap" for kind, _pid in events))
        # Simulate a reused PGID appearing only after reap. Never signal it.
        events.clear()

        def reused_group(pgid: int) -> bool:
            self.assertIn(("reap", pgid), events)
            return True

        with (
            mock.patch.object(_proc.subprocess, "Popen", side_effect=launch),
            mock.patch.object(_proc.os, "killpg", side_effect=signal_group),
            mock.patch.object(_proc, "_group_exists", side_effect=reused_group),
            mock.patch.object(_proc, "REAP_DEADLINE_SECONDS", 0.05),
        ):
            result = _proc.run_reaped([sys.executable, "-c", "pass"], timeout=2)
        self.assertIsInstance(result, _proc.ReapedFailure)
        self.assertFalse(result.termination_confirmed)
        for error in (
            OSError("unsupported"),
            RuntimeError("observer infrastructure failed"),
        ):
            with (
                mock.patch.object(_proc, "_HeldExit", side_effect=error),
                mock.patch.object(_proc.os, "killpg") as signals,
            ):
                result = _proc.run_reaped([sys.executable, "-c", "pass"], timeout=2)
            self.assertIsInstance(result, _proc.ReapedFailure)
            self.assertFalse(result.termination_confirmed)
            signals.assert_not_called()
        with (
            mock.patch.object(_proc.signal, "getsignal", return_value=signal.SIG_IGN),
            mock.patch.object(_proc.os, "killpg") as signals,
        ):
            result = _proc.run_reaped([sys.executable, "-c", "pass"], timeout=2)
        self.assertIsInstance(result, _proc.ReapedFailure)
        self.assertFalse(result.termination_confirmed)
        signals.assert_not_called()

    def test_unlocked_review_probes_cannot_park_or_claim_new_generation(self) -> None:
        for drift in (False, True):
            with self.subTest(drift=drift):
                self.request = build.BuildRequest(
                    "--executor crew:executor --seats sol task",
                    f"probe-generation-{drift}",
                )
                step = self.draft()
                item = step.work_items[0]
                entered, release = threading.Event(), threading.Event()
                provider = mock.Mock()
                provider.is_available.return_value = (True, "")

                def effective(timeout: int) -> int:
                    entered.set()
                    self.assertTrue(release.wait(5))
                    return timeout + int(drift)

                provider.effective_timeout.side_effect = effective
                with (
                    mock.patch.object(
                        review, "_frozen_external_provider", return_value=provider
                    ),
                    concurrent.futures.ThreadPoolExecutor() as pool,
                ):
                    pending = pool.submit(
                        review.execute_external_review, item.review_ref, item.action_id
                    )
                    self.assertTrue(entered.wait(3))
                    try:
                        build.park_review_timeout(
                            step.ref, "injected concurrent floor drift"
                        )
                        parked = build.next_build(step.ref)
                        step = self.decision(
                            parked,
                            "fresh_review",
                            confirmation="fresh_review",
                            workspace_sha256=parked.question.workspace_sha256,
                        )
                        self.assertEqual(
                            self.state()["bl_workflow"]["review_generation"], 2
                        )
                        before = self.state()
                    finally:
                        release.set()
                    with self.assertRaises(review.WorkflowError) as refused:
                        pending.result(5)
                    self.assertIn(refused.exception.code, {"stale_owner", "stale_ref"})
                provider.run.assert_not_called()
                self.assertEqual(self.state(), before)
                self.assertEqual(build.next_build(step.ref).type, "work_batch")
                self.assertFalse(self.state()["awaiting_input"])
                build.cancel_build(step.ref)

    def test_large_feedback_references_survive_retry_and_file_recovery(self) -> None:
        from multiagent.providers import agy, cursor

        directory = self.root / ".crew"
        directory.mkdir()
        (directory / "config.toml").write_text(
            "[build]\nexecutor_retries = 1\nresume_executor = false\n"
            '[seats.fixture-agy]\nmodel = "fixture"\nvia = ["agy"]\n'
            '[seats.fixture-cursor]\nmodel = "fixture"\nvia = ["cursor"]\n'
        )
        config._reset_cache_for_tests()
        report = b"Full immutable evidence remains here.\n" * 10000 + VALID
        self.assertGreater(len(report), 330000)
        for channel, module in (("agy", agy), ("cursor", cursor)):
            with self.subTest(channel=channel):
                self.request = build.BuildRequest(
                    f"--executor fixture-{channel} --seats opus,sonnet task",
                    f"large-feedback-{channel}",
                )
                provider = build.get_provider("fixture-" + channel)
                retained: dict[Path, bytes] = {}
                calls = []

                def launch(
                    argv: list[str], **_kwargs: object
                ) -> tuple[int, bytes, bytes]:
                    calls.append(argv)
                    if len(calls) == 2:
                        next(iter(retained)).unlink()
                        return 1, b"", b"injected terminated failure"
                    return 0, COMPLETED, b""

                with (
                    mock.patch.object(build, "get_provider", return_value=provider),
                    mock.patch.object(
                        provider, "is_available", return_value=(True, "")
                    ),
                    mock.patch.object(module, "run_reaped", side_effect=launch),
                    contextlib.ExitStack() as stack,
                ):
                    if channel == "cursor":
                        stack.enter_context(
                            mock.patch.object(
                                provider,
                                "_supports_continuation_runtime",
                                return_value=False,
                            )
                        )
                        stack.enter_context(
                            mock.patch.object(
                                provider,
                                "_supports_stream_json_runtime",
                                return_value=False,
                            )
                        )
                    step = self.start()
                    step = build.execute_build_action(
                        step.ref, step.work_items[0].action_id
                    )
                    with mock.patch.dict(globals(), {"VALID": report}):
                        step = self.panel(step, "REVISE")
                    item = step.work_items[0]
                    self.assertEqual(item.kind, "revision")
                    prompt = Path(item.prompt_path).read_bytes()
                    self.assertLess(len(prompt), 16 * 1024)
                    self.assertNotIn(b"Full immutable evidence remains here.", prompt)
                    for name in self.state()["bl_workflow"]["feedback_paths"]:
                        path = build._safe_feedback(name)
                        retained[path] = path.read_bytes()
                        self.assertIn(str(path).encode(), prompt)
                    self.assertIn(b"Read retained full feedback file", prompt)
                    step = build.execute_build_action(step.ref, item.action_id)
                    self.assertEqual(step.question.kind, "feedback_recovery")
                    self.assertEqual(len(calls), 2)
                    for path, content in retained.items():
                        if not path.exists():
                            path.write_bytes(content)
                    step = self.decision(step, "retry_feedback")
                    self.assertEqual(
                        Path(step.work_items[0].prompt_path).read_bytes(), prompt
                    )
                    missing = next(iter(retained))
                    missing.unlink()
                    step = build.execute_build_action(
                        step.ref, step.work_items[0].action_id
                    )
                    self.assertEqual(step.question.kind, "feedback_recovery")
                    self.assertEqual(len(calls), 2)
                    self.assertIsNone(self.state()["bl_workflow"]["outstanding_writer"])
                    missing.write_bytes(retained[missing])
                    step = self.decision(step, "retry_feedback")
                    step = build.execute_build_action(
                        step.ref, step.work_items[0].action_id
                    )
                    self.assertEqual(len(calls), 3)
                    self.assertEqual(step.work_items[0].kind, "reviewer")
                    self.assertTrue(
                        all(
                            path.read_bytes() == content
                            for path, content in retained.items()
                        )
                    )
                    self.assertEqual(self.state()["bl_workflow"]["round_attempt"], 2)
                build.cancel_build(step.ref)

    def test_delayed_timeout_probe_does_not_pause_already_claimed_reviewer(
        self,
    ) -> None:
        self.request = dataclasses.replace(
            self.request, raw_arguments="--executor crew:executor --seats sol task"
        )
        step = self.draft()
        item = step.work_items[0]
        probing, release_probe, running, release_run = (
            threading.Event() for _ in range(4)
        )
        delayed, current = mock.Mock(), mock.Mock()
        delayed.is_available.return_value = current.is_available.return_value = (
            True,
            "",
        )

        def effective(timeout: int) -> int:
            probing.set()
            self.assertTrue(release_probe.wait(5))
            return timeout + 100

        def run(*_args: object, **_kwargs: object) -> ProviderResult:
            running.set()
            self.assertTrue(release_run.wait(5))
            return ProviderResult(
                item.review_item.seat, item.model, True, VALID.decode(), None, 0.0
            )

        delayed.effective_timeout.side_effect = effective
        current.effective_timeout.side_effect = lambda timeout: timeout
        current.run.side_effect = run
        with (
            mock.patch.object(
                review, "_frozen_external_provider", side_effect=[delayed, current]
            ),
            concurrent.futures.ThreadPoolExecutor() as pool,
        ):
            older = pool.submit(
                review.execute_external_review, item.review_ref, item.action_id
            )
            self.assertTrue(probing.wait(3))
            newer = pool.submit(
                review.execute_external_review, item.review_ref, item.action_id
            )
            try:
                self.assertTrue(running.wait(3))
                before = self.state()
                release_probe.set()
                self.assertEqual(older.result(3).type, "waiting")
                self.assertEqual(self.state(), before)
                self.assertIsNone(self.state()["bl_workflow"]["question"])
                delayed.run.assert_not_called()
            finally:
                release_probe.set()
                release_run.set()
            newer.result(3)
        self.assertEqual(current.run.call_count, 1)

    def test_inactive_planning_safety_exit_does_not_veto_new_build(self) -> None:
        for marker in ({"force_exit": True}, {"exit_kind": "force_exit"}):
            with self.subTest(marker=marker):
                loop_state.initialize("plan", self.request.session_id, journal={})
                path = loop_state.resolve("mt", self.request.session_id)
                loop_state.mutate(path, lambda data: dict(data, active=False, **marker))
                before = path.read_bytes()
                with self.assertRaises(loop_state.LoopStateError) as refused:
                    loop_state.initialize(
                        "replacement plan",
                        self.request.session_id,
                        journal={},
                        previous=loop_state.read(path),
                    )
                self.assertEqual(refused.exception.code, "active_request_conflict")
                step = self.start()
                self.assertEqual(step.type, "work_batch")
                self.assertEqual(path.read_bytes(), before)
                build.cancel_build(step.ref)
                path.unlink()
        loop_state.initialize("active plan", self.request.session_id, journal={})
        with self.assertRaises(loop_state.LoopStateError):
            self.start()
        loop_state.mutate(
            loop_state.resolve("mt", self.request.session_id),
            lambda data: dict(data, active=False),
        )
        step = self.start()
        build.claim_build_action(step.ref, step.work_items[0].action_id)
        build.cancel_build(step.ref)
        with self.assertRaises(loop_state.LoopStateError) as fenced:
            self.start()
        self.assertEqual(fenced.exception.code, "outstanding_writer")

    def test_recurring_route_questions_have_durable_identity_and_old_answer_replay(
        self,
    ) -> None:
        step = self.start()
        original_item = step.work_items[0]
        decisions = []
        ids = []
        for occurrence in range(3):
            with mock.patch.object(build, "_route_matches", return_value=False):
                parked = build.next_build(step.ref)
            self.assertEqual(parked.question.kind, "route_unavailable")
            ids.append(parked.question.question_id)
            self.assertEqual(len(set(ids)), occurrence + 1)
            self.assertEqual(
                build.resume_build(self.request.session_id).question, parked.question
            )
            before = self.state()
            for previous in decisions:
                replay = build.decide_build(previous)
                self.assertEqual(replay.question, parked.question)
                self.assertEqual(self.state(), before)
            self.assertEqual(build.next_build(step.ref).question, parked.question)
            decision = build.BuildDecision(
                step.ref, parked.question.question_id, "retry_route"
            )
            step = build.decide_build(decision)
            decisions.append(decision)
            self.assertEqual(step.work_items, (original_item,))
            self.assertFalse(self.state()["awaiting_input"])
            self.assertIsNone(self.state()["bl_workflow"]["outstanding_writer"])
            self.assertEqual(
                len(self.state()["bl_workflow"]["decisions"]), occurrence + 1
            )
        before = self.state()
        for decision in decisions:
            self.assertEqual(build.decide_build(decision).work_items, (original_item,))
        self.assertEqual(self.state(), before)
        self.assertEqual(self.state()["bl_workflow"]["action_ordinal"], 1)
        self.assertEqual(self.state()["bl_workflow"]["round_attempt"], 1)
        with self.assertRaisesRegex(review.WorkflowError, "accepted decision"):
            build.decide_build(dataclasses.replace(decisions[0], kind="cancel"))

    def test_unsuccessful_feedback_repairs_reissue_distinct_questions_once(
        self,
    ) -> None:
        step = self.draft()
        original_write = build._write_once
        publications = []

        def unavailable(path: Path, content: bytes, ref: build.BuildRef) -> None:
            if path.parent.name == "prompts":
                publications.append(path)
                raise OSError("feedback repair still unavailable")
            original_write(path, content, ref)

        decisions = []
        ids = []
        with mock.patch.object(build, "_write_once", side_effect=unavailable):
            step = self.panel(step, "REVISE")
            preserved = self.state()
            for occurrence in range(3):
                self.assertEqual(step.question.kind, "feedback_recovery")
                ids.append(step.question.question_id)
                self.assertEqual(len(set(ids)), occurrence + 1)
                before = self.state()
                writes = len(publications)
                for previous in decisions:
                    self.assertEqual(
                        build.decide_build(previous).question, step.question
                    )
                self.assertEqual(self.state(), before)
                self.assertEqual(len(publications), writes)
                decision = build.BuildDecision(
                    step.ref, step.question.question_id, "retry_feedback"
                )
                step = build.decide_build(decision)
                decisions.append(decision)
                self.assertEqual(len(publications), writes + 1)
                self.assertNotIn(step.question.question_id, ids)
                self.assertEqual(self.state()["bl_workflow"]["round_attempt"], 0)
                self.assertEqual(self.state()["bl_workflow"]["action_ordinal"], 1)
            ids.append(step.question.question_id)
        # This decision repairs the latest event; older accepted answers remain replays.
        decision = build.BuildDecision(
            step.ref, step.question.question_id, "retry_feedback"
        )
        step = build.decide_build(decision)
        decisions.append(decision)
        self.assertEqual(step.work_items[0].kind, "revision")
        current = self.state()
        for key in (
            "revision_round",
            "stop_fires",
            "max_stop_fires",
            "started_at",
            "deadline_minutes",
            "no_deadline",
        ):
            self.assertEqual(current[key], preserved[key])
        for key in ("applied_outcomes", "accepted_actions", "review_generation"):
            self.assertEqual(current["bl_workflow"][key], preserved["bl_workflow"][key])
        self.assertEqual(current["bl_workflow"]["action_ordinal"], 2)
        self.assertEqual(current["bl_workflow"]["round_attempt"], 1)
        for decision in decisions:
            self.assertEqual(build.decide_build(decision).work_items, step.work_items)
        self.assertEqual(self.state(), current)
        self.assertEqual(len(current["bl_workflow"]["decisions"]), 4)

    def test_invalid_utf8_retry_feedback_keeps_original_and_completes_both_routes(
        self,
    ) -> None:
        from multiagent.providers import codex

        directory = self.root / ".crew"
        directory.mkdir()
        (directory / "config.toml").write_text("[build]\nresume_executor = false\n")
        config._reset_cache_for_tests()
        report = (
            "Original report includes readable Unicode: café.\n".encode() * 8000
            + b"\xff\xfe\nCREW_BUILD_STATUS: COMPLETED\n"
        )
        self.assertGreater(len(report), 340000)
        for external in (False, True):
            for kind in ("retry_executor", "answer_executor"):
                with self.subTest(external=external, kind=kind):
                    self.request = build.BuildRequest(
                        f"--executor {'sol' if external else 'crew:executor'} --seats opus task",
                        f"utf8-recovery-{external}-{kind}",
                    )
                    provider = build.get_provider("sol")
                    with (
                        mock.patch.object(build, "get_provider", return_value=provider),
                        mock.patch.object(
                            provider, "is_available", return_value=(True, "")
                        ),
                        mock.patch.object(
                            codex, "run_reaped", return_value=(0, report, b"")
                        ) as runner,
                    ):
                        step = self.start()
                        first = step.work_items[0].action_id
                        if external:
                            step = build.execute_build_action(step.ref, first)
                        else:
                            build.claim_build_action(step.ref, first)
                            step = build.capture_build_return(step.ref, first, report)
                    self.assertEqual(step.question.kind, "executor_report_recovery")
                    root = build._root(step.ref)
                    original = root / "returns" / f"{first}.txt"
                    retained = {
                        p: (p.read_bytes(), p.stat().st_mtime_ns)
                        for p in root.rglob("*")
                        if p.is_file()
                        and p.parent.name
                        in {"returns", "execution-receipts", "submissions"}
                    }
                    decision = build.BuildDecision(
                        step.ref,
                        step.question.question_id,
                        kind,
                        confirmation="stack_edits",
                        workspace_sha256=step.question.workspace_sha256,
                        answer="Resolve the recorded blocker and correct report encoding"
                        if kind == "answer_executor"
                        else None,
                    )
                    before_publication = self.state()
                    original_write = build._write_once

                    def fail_publication(
                        path: Path, content: bytes, ref: build.BuildRef
                    ) -> None:
                        if path.parent.name == "feedback":
                            raise OSError("readable feedback publication failed")
                        original_write(path, content, ref)

                    with (
                        mock.patch.object(
                            build, "_write_once", side_effect=fail_publication
                        ),
                        self.assertRaisesRegex(OSError, "publication failed"),
                    ):
                        build.decide_build(decision)
                    self.assertEqual(self.state(), before_publication)
                    step = build.decide_build(decision)
                    self.assertEqual(step.type, "work_batch")
                    second = step.work_items[0].action_id
                    self.assertEqual(second, "action-0002")
                    supplement = Path(self.state()["bl_workflow"]["feedback_paths"][-1])
                    self.assertEqual(supplement, root / "feedback" / f"{first}.txt")
                    text = supplement.read_text(encoding="utf-8")
                    for value in (
                        "INVALID UTF-8 EXECUTOR REPORT",
                        "Recorded result status: invalid_report",
                        str(original),
                        build.sha256(report),
                        first,
                        step.ref.loop_instance_id,
                        "café",
                        "\\xff\\xfe",
                    ):
                        self.assertIn(value, text)
                    self.assertIn("| CREW_BUILD_STATUS: COMPLETED", text)
                    self.assertEqual(
                        build.report_status(supplement.read_bytes()), "invalid_report"
                    )
                    prompt = Path(step.work_items[0].prompt_path).read_bytes()
                    self.assertLess(len(prompt), 16000)
                    self.assertIn(str(supplement).encode(), prompt)
                    self.assertNotIn(
                        b"Original report includes readable Unicode", prompt
                    )
                    before = self.state()
                    self.assertEqual(
                        build.decide_build(decision).work_items, step.work_items
                    )
                    self.assertEqual(self.state(), before)
                    # A lost readable projection is deterministically rebuilt from verified original bytes.
                    projected_bytes = supplement.read_bytes()
                    supplement.unlink()
                    with (
                        mock.patch.object(build, "get_provider", return_value=provider),
                        mock.patch.object(
                            provider, "is_available", return_value=(True, "")
                        ),
                        mock.patch.object(
                            codex, "run_reaped", return_value=(0, COMPLETED, b"")
                        ) as runner,
                    ):
                        if external:
                            step = build.execute_build_action(step.ref, second)
                            self.assertEqual(runner.call_count, 1)
                        else:
                            self.assertEqual(
                                build.claim_build_action(step.ref, second)[
                                    "authorization"
                                ],
                                "spawn",
                            )
                            step = build.capture_build_return(
                                step.ref, second, COMPLETED
                            )
                            runner.assert_not_called()
                    self.assertEqual(supplement.read_bytes(), projected_bytes)
                    self.assertEqual(step.work_items[0].kind, "reviewer")
                    self.assertEqual(
                        [
                            r["status"]
                            for r in self.state()["bl_workflow"]["accepted_actions"]
                        ],
                        ["invalid_report", "completed"],
                    )
                    for path, (content, mtime) in retained.items():
                        self.assertEqual(path.read_bytes(), content)
                        self.assertEqual(path.stat().st_mtime_ns, mtime)
                    self.assertEqual(original.read_bytes(), report)
                    build.cancel_build(step.ref)

    def test_invalid_utf8_companion_rejects_damaged_or_foreign_evidence(self) -> None:
        from multiagent.providers import codex

        directory = self.root / ".crew"
        directory.mkdir()
        (directory / "config.toml").write_text("[build]\nresume_executor = false\n")
        config._reset_cache_for_tests()
        for external in (False, True):
            for fault in (
                "original_missing",
                "original_changed",
                "companion_changed",
                "companion_link",
            ):
                with self.subTest(external=external, fault=fault):
                    self.request = build.BuildRequest(
                        f"--executor {'sol' if external else 'crew:executor'} --seats opus task",
                        f"utf8-guard-{external}-{fault}",
                    )
                    provider = build.get_provider("sol")
                    report = b"Missing acceptance input\n\xff\nCREW_BUILD_STATUS: COMPLETED\n"
                    with (
                        mock.patch.object(build, "get_provider", return_value=provider),
                        mock.patch.object(
                            provider, "is_available", return_value=(True, "")
                        ),
                        mock.patch.object(
                            codex, "run_reaped", return_value=(0, report, b"")
                        ),
                    ):
                        step = self.start()
                        first = step.work_items[0].action_id
                        if external:
                            step = build.execute_build_action(step.ref, first)
                        else:
                            build.claim_build_action(step.ref, first)
                            step = build.capture_build_return(step.ref, first, report)
                    step = self.decision(
                        step,
                        "retry_executor",
                        confirmation="stack_edits",
                        workspace_sha256=step.question.workspace_sha256,
                    )
                    original = build._root(step.ref) / "returns" / f"{first}.txt"
                    companion = Path(self.state()["bl_workflow"]["feedback_paths"][-1])
                    saved = companion.read_bytes()
                    if fault == "original_missing":
                        original.unlink()
                    elif fault == "original_changed":
                        original.write_bytes(b"foreign result")
                    elif fault == "companion_changed":
                        companion.write_bytes(b"foreign readable feedback")
                    else:
                        foreign = self.home / "foreign-feedback.txt"
                        foreign.write_bytes(saved)
                        companion.unlink()
                        companion.symlink_to(foreign)
                    action = step.work_items[0].action_id
                    with (
                        mock.patch.object(build, "get_provider", return_value=provider),
                        mock.patch.object(
                            provider, "is_available", return_value=(True, "")
                        ),
                        mock.patch.object(codex, "run_reaped") as runner,
                    ):
                        if fault == "original_missing":
                            if external:
                                guarded = build.execute_build_action(step.ref, action)
                            else:
                                guarded = build.claim_build_action(step.ref, action)[
                                    "step"
                                ]
                                self.assertEqual(
                                    guarded["question"]["kind"], "feedback_recovery"
                                )
                            if external:
                                self.assertEqual(
                                    guarded.question.kind, "feedback_recovery"
                                )
                        else:
                            with self.assertRaises(review.WorkflowError):
                                if external:
                                    build.execute_build_action(step.ref, action)
                                else:
                                    build.claim_build_action(step.ref, action)
                        runner.assert_not_called()
                    self.assertIsNone(self.state()["bl_workflow"]["outstanding_writer"])
                    self.assertEqual(
                        self.state()["bl_workflow"]["action"]["status"], "ready"
                    )
                    original.write_bytes(report)
                    if companion.is_symlink():
                        companion.unlink()
                    companion.write_bytes(saved)
                    if fault == "original_missing":
                        current = build.next_build(step.ref)
                        step = self.decision(current, "retry_feedback")
                    self.assertEqual(build.next_build(step.ref).type, "work_batch")
                    build.cancel_build(step.ref)

    def test_route_drift_recovery_has_no_claim(self) -> None:
        step = self.start()
        with mock.patch.object(build, "_route_matches", return_value=False):
            step = build.next_build(step.ref)
            self.assertEqual(step.question.kind, "route_unavailable")
            with self.assertRaises(review.WorkflowError):
                self.decision(step, "retry_route")
        self.assertEqual(self.decision(step, "retry_route").type, "work_batch")
        self.assertIsNone(self.state()["bl_workflow"]["outstanding_writer"])

    def test_guard_recheck_maps_original_result_and_feedback_recovery(self) -> None:
        original_branch = execution.observe_workspace(str(self.root)).branch
        step = self.start()
        action = step.work_items[0].action_id
        build.claim_build_action(step.ref, action)
        subprocess.run(
            ["git", "symbolic-ref", "HEAD", "refs/heads/changed"],
            cwd=self.root,
            check=True,
        )
        step = build.capture_build_return(step.ref, action, COMPLETED)
        subprocess.run(
            ["git", "symbolic-ref", "HEAD", "refs/heads/" + original_branch],
            cwd=self.root,
            check=True,
        )
        step = self.decision(step, "recheck_workspace")
        self.assertEqual(step.work_items[0].kind, "reviewer")
        step = self.panel(step, "REVISE")
        # Damage context only after a verdict; replay never purchases another panel.
        for path in self.state()["bl_workflow"]["feedback_paths"]:
            Path(path).write_bytes(b"\xff")

        def unissue(_data: dict, journal: build.BuildJournal) -> None:
            journal.action = None
            journal.round_prompt_path = None
            journal.round_prompt_sha256 = None

        build._transaction(step.ref, unissue)
        step = build.next_build(step.ref)
        self.assertEqual(step.question.kind, "feedback_recovery")
        revision = self.state()["revision_round"]
        for path in self.state()["bl_workflow"]["feedback_paths"]:
            Path(path).write_text("Retained repaired findings")
        step = self.decision(step, "retry_feedback")
        self.assertEqual(step.work_items[0].kind, "revision")
        self.assertEqual(self.state()["revision_round"], revision)

    def test_partial_panel_retry_preserves_success_and_force_disclosures(self) -> None:
        step = self.panel(self.draft(), failed=1)
        self.assertEqual(step.question.kind, "completion_advisory")
        self.assertIn("quorum-not-met", step.question.advisories)
        self.assertNotIn("measure-twice", step.question.text)
        evidence = review._read_loop_review_evidence_under_owner_lock(
            step.question.review_ref
        )
        invalid_binding = {**self.state(), "phase": "drafting"}
        with self.assertRaisesRegex(
            loop_state.LoopStateError, "build-next with the issued BuildRef"
        ):
            loop_state.apply_verdict(invalid_binding, evidence, "APPROVED", loop="bl")
        step = self.decision(step, "retry_review")
        self.assertEqual(len(step.work_items), 1)
        self.assertEqual(step.work_items[0].kind, "reviewer")
        done = self.panel(step)
        self.assertEqual(done.type, "terminal")
        self.request = dataclasses.replace(self.request, session_id="force")
        step = self.panel(self.draft(), failed=1)
        old_question = step.question.question_id
        (self.root / "drift.txt").write_text("changed")
        step = self.decision(step, "force", confirmation="force")
        self.assertNotEqual(step.question.question_id, old_question)
        self.assertIn("target-drift", step.question.advisories)
        self.assertIn("build-decide", step.question.text)
        self.assertNotIn("measure-twice", step.question.text)
        done = self.decision(step, "force", confirmation="force")
        self.assertEqual(done.type, "terminal")
        self.assertIn("target-drift", self.state()["last_verdict_overrides"])
        self.assertEqual(
            done.outcome["last_verdict_overrides"],
            self.state()["last_verdict_overrides"],
        )
        self.assertIn("target-drift", done.display)
        self.assertEqual(
            build.resume_build(self.request.session_id).outcome, done.outcome
        )

    def test_exact_reports_through_provider_binaries_preserve_dispatch_shape(
        self,
    ) -> None:
        binary_dir = self.home / "bin"
        binary_dir.mkdir()
        report = self.home / "report.txt"
        binary_source = (
            f"#!{sys.executable}\n"
            "import os, pathlib, sys\n"
            "if sys.argv[1:] == ['--version']:\n"
            "    print('2026.10.02-fixture'); raise SystemExit(0)\n"
            "raw = pathlib.Path(os.environ['CREW_TEST_REPORT']).read_bytes()\n"
            "if '-o' in sys.argv:\n"
            "    pathlib.Path(sys.argv[sys.argv.index('-o') + 1]).write_bytes(raw)\n"
            "else:\n"
            "    sys.stdout.buffer.write(raw)\n"
        )
        for name in ("codex", "agy", "agent"):
            path = binary_dir / name
            path.write_text(binary_source)
            path.chmod(0o755)
        crew_dir = self.root / ".crew"
        crew_dir.mkdir()
        (crew_dir / "config.toml").write_text(
            "[build]\nresume_executor = false\n"
            '[seats.fixture-codex]\nmodel = "fixture"\nvia = ["codex"]\n'
            '[seats.fixture-agy]\nmodel = "fixture"\nvia = ["agy"]\n'
            '[seats.fixture-cursor]\nmodel = "fixture"\nvia = ["cursor"]\n'
        )
        config._reset_cache_for_tests()
        reports = (
            (b"Implemented\nCREW_BUILD_STATUS: COMPLETED\n", "completed"),
            (b"Implemented\r\nCREW_BUILD_STATUS: COMPLETED\r\n", "completed"),
            (b"CREW_BUILD_STATUS: COMPLETED \n", "invalid_report"),
            (b" CREW_BUILD_STATUS: COMPLETED", "invalid_report"),
            (b"\tCREW_BUILD_STATUS: COMPLETED", "invalid_report"),
            (b"CREW_BUILD_STATUS: COMPLETED extra", "invalid_report"),
            ("CREW_BUILD_STATUS: COMPLETED\u2028".encode(), "invalid_report"),
            (b"CREW_BUILD_STATUS: BLOCKED\r\n", "blocked"),
        )
        with mock.patch.dict(
            os.environ,
            {
                "PATH": str(binary_dir) + os.pathsep + os.environ["PATH"],
                "CREW_TEST_REPORT": str(report),
            },
        ):
            for channel in ("codex", "agy", "cursor"):
                for index, (content, expected) in enumerate(reports):
                    with self.subTest(channel=channel, report=index):
                        seat = "fixture-" + channel
                        self.request = build.BuildRequest(
                            f"--executor {seat} --seats opus task",
                            f"exact-{channel}-{index}",
                        )
                        report.write_bytes(content)
                        step = self.start()
                        item = step.work_items[0]
                        step = build.execute_build_action(step.ref, item.action_id)
                        state = self.state()
                        self.assertEqual(
                            state["bl_workflow"]["accepted_actions"][-1]["status"],
                            expected,
                        )
                        self.assertEqual(
                            (
                                build._root(step.ref)
                                / "returns"
                                / f"{item.action_id}.txt"
                            ).read_bytes(),
                            content,
                        )
                        if expected == "completed":
                            self.assertEqual(step.work_items[0].kind, "reviewer")
                        else:
                            self.assertEqual(
                                step.question.kind,
                                "executor_blocked"
                                if expected == "blocked"
                                else "executor_report_recovery",
                            )
                            self.assertEqual(
                                state["bl_workflow"]["review_generation"], 0
                            )
                        provider = build.get_provider(seat)
                        returned = provider.run(
                            "fixture",
                            sandbox="workspace-write",
                            workspace=str(self.root),
                        )
                        self.assertEqual(returned.exact_output, content)
                        self.assertEqual(
                            set(returned.to_dict()),
                            {"name", "model", "ok", "output", "error", "elapsed"},
                        )
                        envelope = execution.execute_write(
                            execution.WriteRequest(
                                seat,
                                "fixture",
                                None,
                                30,
                                {},
                                str(self.root),
                                "dispatch-fixture",
                                None,
                                "",
                            ),
                            provider=provider,
                        )
                        self.assertEqual(envelope["output"], returned.output)
                        self.assertEqual(len(envelope), 16)

    def test_retry_floor_drift_fresh_generation_clears_old_authorization(self) -> None:
        self.request = dataclasses.replace(
            self.request, raw_arguments="--executor crew:executor --seats sol,luna task"
        )

        def external_panel(step: build.BuildStep, failed: int = 0) -> build.BuildStep:
            provider = mock.Mock()
            provider.is_available.return_value = (True, "")
            provider.effective_timeout.side_effect = lambda timeout: timeout
            with mock.patch.object(
                review, "_frozen_external_provider", return_value=provider
            ):
                for index, item in enumerate(step.work_items):
                    provider.effective_timeout.side_effect = lambda base: max(
                        base, item.timeout_seconds
                    )
                    provider.run.return_value = ProviderResult(
                        item.review_item.seat,
                        item.model,
                        index >= failed,
                        VALID.decode(),
                        "outage" if index < failed else None,
                        0.0,
                    )
                    review.execute_external_review(item.review_ref, item.action_id)
            step = build.next_build(step.ref)
            if step.work_items:
                item = step.work_items[0]
                self.assertEqual(item.kind, "synthesis")
                review.claim_review_action(
                    review.ClaimRequest(item.review_ref, item.action_id)
                )
                transport.capture_review_return(
                    item.review_ref,
                    item.action_id,
                    b"All findings assessed\nBLOCKING_CAUSES: 0\n",
                    judgment=transport.SynthesisJudgment("APPROVED", False),
                )
                step = build.next_build(step.ref)
            return step

        step = external_panel(self.draft(), failed=1)
        step = self.decision(step, "retry_review")
        self.assertIsNotNone(self.state()["bl_workflow"]["retry_authorization"])
        item = step.work_items[0]
        provider = mock.Mock()
        provider.is_available.return_value = (True, "")
        provider.effective_timeout.side_effect = lambda timeout: timeout + 100
        with (
            mock.patch.object(
                review, "_frozen_external_provider", return_value=provider
            ),
            self.assertRaisesRegex(review.WorkflowError, "fresh review decision"),
        ):
            review.execute_external_review(item.review_ref, item.action_id)
        provider.run.assert_not_called()
        parked = build.next_build(step.ref)
        self.assertEqual(parked.question.kind, "review_timeout_changed")
        first_generation = self.state()["bl_workflow"]["review_generation"]
        with mock.patch.object(
            review, "_provider_timeout", return_value=item.timeout_seconds + 100
        ):
            step = self.decision(
                parked,
                "fresh_review",
                confirmation="fresh_review",
                workspace_sha256=parked.question.workspace_sha256,
            )
        state = self.state()
        self.assertIsNone(state["bl_workflow"]["retry_authorization"])
        self.assertIsNone(state["bl_workflow"]["question"])
        self.assertFalse(state["awaiting_input"])
        self.assertEqual(
            state["bl_workflow"]["review_generation"], first_generation + 1
        )
        self.assertEqual(build.next_build(step.ref).type, "work_batch")
        self.assertEqual(external_panel(step).type, "terminal")

    def test_build_reports_do_not_turn_authentication_prose_into_transport_failure(
        self,
    ) -> None:
        from multiagent.providers import ProviderContinuation, agy, cursor

        crew_dir = self.root / ".crew"
        crew_dir.mkdir()
        (crew_dir / "config.toml").write_text(
            "[build]\nexecutor_retries = 2\n"
            '[seats.fixture-agy]\nmodel = "fixture"\nvia = ["agy"]\n'
            '[seats.fixture-cursor]\nmodel = "fixture"\nvia = ["cursor"]\n'
        )
        config._reset_cache_for_tests()
        conversation = "12345678-1234-1234-1234-123456789abc"
        for mode in ("agy", "cursor-plain", "cursor-stream"):
            channel = "agy" if mode == "agy" else "cursor"
            module = agy if channel == "agy" else cursor
            chained = mode == "cursor-stream"
            for case in (
                "blocked",
                "completed",
                "banner",
                "stderr_auth",
                "nonzero",
                "cancelled",
                "failed_terminal",
            ):
                if case == "failed_terminal" and not chained:
                    continue
                with self.subTest(mode=mode, case=case):
                    self.request = build.BuildRequest(
                        f"--executor fixture-{channel} --seats opus task",
                        f"auth-report-{mode}-{case}",
                    )
                    report = (
                        b'The application displays "please sign in"; authentication required / OAuth error handling needs attention.\r\n'
                        + (
                            b"CREW_BUILD_STATUS: BLOCKED\r\n"
                            if case == "blocked"
                            else b"CREW_BUILD_STATUS: COMPLETED\r\n"
                        )
                    )
                    if case == "banner":
                        report = b"Please sign in at https://accounts.google.com/o/oauth2/auth; authentication timed out"
                    wire = report
                    if chained:
                        wire = b"".join(
                            json.dumps(event).encode() + b"\n"
                            for event in (
                                {
                                    "type": "system",
                                    "subtype": "init",
                                    "session_id": conversation,
                                },
                                {
                                    "type": "result",
                                    "subtype": "error"
                                    if case == "failed_terminal"
                                    else "success",
                                    "is_error": case == "failed_terminal",
                                    "session_id": conversation,
                                    "result": report.decode(),
                                    "error": "authentication required"
                                    if case == "failed_terminal"
                                    else "",
                                },
                            )
                        )
                    rc = 1 if case == "nonzero" else -15 if case == "cancelled" else 0
                    stderr = (
                        b"please sign in: authentication required"
                        if case == "stderr_auth"
                        else b""
                    )
                    provider = build.get_provider("fixture-" + channel)
                    with (
                        mock.patch.object(build, "get_provider", return_value=provider),
                        mock.patch.object(
                            provider, "is_available", return_value=(True, "")
                        ),
                        mock.patch.object(
                            provider,
                            "_supports_continuation_runtime",
                            return_value=chained,
                            create=True,
                        ),
                        mock.patch.object(
                            module, "run_reaped", return_value=(rc, wire, stderr)
                        ) as runner,
                    ):
                        if case in {"blocked", "completed"}:
                            # Ordinary dispatch/read-only classification remains unchanged.
                            ordinary = provider.run(
                                "ordinary",
                                sandbox="workspace-write",
                                continuation=ProviderContinuation()
                                if chained
                                else None,
                            )
                            self.assertFalse(ordinary.ok)
                            self.assertEqual(ordinary.transport_status, "failed")
                            runner.reset_mock()
                        step = self.start()
                        while (
                            step.type == "work_batch"
                            and step.work_items[0].kind == "implementation"
                        ):
                            step = build.execute_build_action(
                                step.ref, step.work_items[0].action_id
                            )
                    state = self.state()["bl_workflow"]
                    expected = (
                        case
                        if case in {"blocked", "completed", "cancelled"}
                        else "failed"
                    )
                    self.assertEqual(
                        {r["status"] for r in state["accepted_actions"]}, {expected}
                    )
                    self.assertEqual(
                        runner.call_count, 3 if expected == "failed" else 1
                    )
                    if expected in {"blocked", "completed"}:
                        self.assertEqual(state["round_attempt"], 1)
                        self.assertEqual(
                            (
                                build._root(step.ref) / "returns/action-0001.txt"
                            ).read_bytes(),
                            report,
                        )
                    if expected == "completed":
                        self.assertEqual(step.work_items[0].kind, "reviewer")
                    else:
                        self.assertEqual(step.type, "needs_input")
                        self.assertEqual(
                            step.question.kind,
                            "executor_blocked"
                            if expected == "blocked"
                            else "execution_recovery",
                        )
                        self.assertEqual(state["review_generation"], 0)

    def test_real_provider_transport_outcomes_control_automatic_retries(self) -> None:
        import shlex

        from multiagent.providers import agy, codex, cursor
        from multiagent.providers._proc import (
            TIMEOUT,
            TIMEOUT_UNCONFIRMED,
            ReapedFailure,
        )

        crew_dir = self.root / ".crew"
        crew_dir.mkdir()
        (crew_dir / "config.toml").write_text(
            "[build]\nexecutor_retries = 2\nresume_executor = false\n"
            '[seats.fixture-codex]\nmodel = "fixture"\nvia = ["codex"]\n'
            '[seats.fixture-agy]\nmodel = "fixture"\nvia = ["agy"]\n'
            '[seats.fixture-cursor]\nmodel = "fixture"\nvia = ["cursor"]\n'
        )
        config._reset_cache_for_tests()
        for channel, module in (("codex", codex), ("agy", agy), ("cursor", cursor)):
            for expected, returned, launches in (
                ("timeout", TIMEOUT, 3),
                ("timeout_unconfirmed", TIMEOUT_UNCONFIRMED, 1),
                (
                    "termination_unconfirmed",
                    ReapedFailure(
                        "process communication failed", COMPLETED, b"", False
                    ),
                    1,
                ),
                ("cancelled", (-15, b"", b"operation cancelled"), 1),
                ("failed", (1, b"", b"invalid timeout configuration"), 3),
                ("unavailable", None, 0),
            ):
                with self.subTest(channel=channel, status=expected):
                    seat = "fixture-" + channel
                    self.request = build.BuildRequest(
                        f"--executor {seat} --seats opus task",
                        f"transport-{channel}-{expected}",
                    )
                    provider = build.get_provider(seat)
                    with (
                        mock.patch.object(build, "get_provider", return_value=provider),
                        mock.patch.object(
                            provider,
                            "is_available",
                            return_value=(returned is not None, "fixture unavailable"),
                        ),
                        mock.patch.object(
                            module, "run_reaped", return_value=returned
                        ) as runner,
                    ):
                        step = self.start()
                        while step.type == "work_batch":
                            self.assertEqual(step.work_items[0].kind, "implementation")
                            step = build.execute_build_action(
                                step.ref, step.work_items[0].action_id
                            )
                    self.assertEqual(runner.call_count, launches)
                    state = self.state()
                    accepted = state["bl_workflow"]["accepted_actions"]
                    if expected in {"timeout_unconfirmed", "termination_unconfirmed"}:
                        self.assertFalse(accepted)
                        self.assertEqual(
                            state["bl_workflow"]["action"]["status"], "claimed"
                        )
                        self.assertEqual(
                            state["bl_workflow"]["outstanding_writer"], "action-0001"
                        )
                        self.assertEqual(step.type, "waiting")
                        self.assertIsNone(step.question)
                        self.assertIn(
                            shlex.join(build.recovery_argv(step.ref, "action-0001")),
                            step.display,
                        )
                        self.assertFalse(state["awaiting_input"])
                        self.assertEqual(state["bl_workflow"]["review_generation"], 0)
                        recovered = build.recover_build_action(
                            step.ref, "action-0001", "not_running"
                        )
                        self.assertEqual(recovered.question.kind, "execution_recovery")
                        self.assertTrue(self.state()["awaiting_input"])
                        continue
                    self.assertEqual(step.question.kind, "execution_recovery")
                    self.assertEqual(len(accepted), max(1, launches))
                    self.assertEqual({item["status"] for item in accepted}, {expected})
                    receipt = json.loads(
                        (
                            build._root(step.ref)
                            / "execution-receipts"
                            / (accepted[-1]["action_id"] + ".json")
                        ).read_bytes()
                    )
                    self.assertEqual(receipt["transport_status"], expected)
                    self.assertEqual(state["bl_workflow"]["review_generation"], 0)
                    if expected == "timeout":
                        self.assertIn("timed out", accepted[-1]["diagnostic"])

    def test_capture_flags_from_build_guidance_settle_executor_and_review(self) -> None:
        instructions = (
            Path(__file__).resolve().parents[2] / "commands/build.md"
        ).read_text()
        executor_flag = re.search(
            r"For executor `build-capture`.*?`(--return-file)", instructions, re.DOTALL
        ).group(1)
        review_flag = re.search(
            r"For review `review-capture`.*?`(--returned-file)", instructions, re.DOTALL
        ).group(1)
        self.assertIn("review host-Write capture with `--returned-file`", instructions)
        self.assertIn("commands.capture already includes", instructions)
        step = self.start()
        item = step.work_items[0]
        build.claim_build_action(step.ref, item.action_id)
        path = Path(item.returned_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(COMPLETED)
        self.assertEqual(item.commands["capture"].count(executor_flag), 1)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main([*item.commands["capture"][1:]]), 0)
        step = build.next_build(step.ref)
        for item in step.work_items:
            review.claim_review_action(
                review.ClaimRequest(item.review_ref, item.action_id)
            )
            path = Path(item.review_item.return_transport["fallback"]["ingress_path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(VALID)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    cli.main([*item.commands["capture"][1:], review_flag, str(path)]), 0
                )
        step = build.next_build(step.ref)
        item = step.work_items[0]
        self.assertEqual(item.kind, "synthesis")
        review.claim_review_action(review.ClaimRequest(item.review_ref, item.action_id))
        path = Path(item.returned_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"All findings assessed\nBLOCKING_CAUSES: 0\n")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                cli.main(
                    [
                        *item.commands["capture"][1:],
                        review_flag,
                        str(path),
                        "--verdict",
                        "APPROVED",
                    ]
                ),
                0,
            )
        self.assertEqual(build.next_build(step.ref).type, "terminal")

    def test_real_group_quiescence_controls_every_writer_settlement(self) -> None:
        import signal

        from multiagent.providers import _proc, codex

        binary_dir = self.home / "bin"
        binary_dir.mkdir()
        crew_dir = self.root / ".crew"
        crew_dir.mkdir()
        (crew_dir / "config.toml").write_text(
            "[build]\nexecutor_retries = 1\nresume_executor = false\n"
        )
        config._reset_cache_for_tests()
        real_killpg = os.killpg
        real_popen = subprocess.Popen

        def alive(pid: int) -> bool:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            return True

        for mode, suppress_kill in (
            (m, k)
            for m in ("timeout", "success", "failure", "communication")
            for k in (False, True)
        ):
            with self.subTest(mode=mode, termination_unconfirmed=suppress_kill):
                case = f"{mode}-{suppress_kill}"
                pidfile = self.home / f"pids-{case}.json"
                ready = self.home / f"ready-{case}"
                heartbeat = self.home / f"heartbeat-{case}"
                stop = self.home / f"stop-{case}"
                child = (
                    "import signal,time\nfrom pathlib import Path\n"
                    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                    f"Path({str(ready)!r}).write_text('ready')\n"
                    f"while not Path({str(stop)!r}).exists():\n"
                    f"    Path({str(heartbeat)!r}).write_text(str(time.time_ns()))\n"
                    "    time.sleep(0.02)\n"
                )
                source = (
                    f"#!{sys.executable}\n"
                    "import json,os,subprocess,sys,time\nfrom pathlib import Path\n"
                    f"ready=Path({str(ready)!r})\nready.unlink(missing_ok=True)\n"
                    f"child=subprocess.Popen([sys.executable,'-c',{child!r}], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
                    "while not ready.exists(): time.sleep(0.01)\n"
                    f"Path({str(pidfile)!r}).write_text(json.dumps({{'parent':os.getpid(),'child':child.pid}}))\n"
                    f"Path(sys.argv[sys.argv.index('-o')+1]).write_bytes({COMPLETED!r})\n"
                    + (
                        "time.sleep(120)\n"
                        if mode == "timeout"
                        else f"sys.exit({1 if mode == 'failure' else 0})\n"
                    )
                )
                executable = binary_dir / "codex"
                executable.write_text(source)
                executable.chmod(0o755)
                processes: list[subprocess.Popen] = []
                escalations: list[bool] = []

                def launch(*args: object, **kwargs: object) -> subprocess.Popen:
                    proc = real_popen(*args, **kwargs)
                    if kwargs.get("start_new_session"):
                        processes.append(proc)
                        if mode == "communication":
                            communicate = proc.communicate

                            def broken_communication(
                                *args: object, **kwargs: object
                            ) -> object:
                                communicate(*args, **kwargs)
                                raise OSError("injected communication failure")

                            proc.communicate = broken_communication
                    return proc

                def signal_group(pgid: int, sig: int) -> None:
                    if sig == signal.SIGKILL:
                        owned = next(p for p in processes if p.pid == pgid)
                        escalations.append(
                            owned.returncode is None and owned.poll() is not None
                        )
                        if suppress_kill:
                            return
                    real_killpg(pgid, sig)

                def bounded_run(*args: object, **kwargs: object) -> object:
                    return _proc.run_reaped(*args, **{**kwargs, "timeout": 0.4})

                self.request = build.BuildRequest(
                    "--executor sol --seats opus task", f"group-{case}"
                )
                try:
                    with (
                        mock.patch.dict(
                            os.environ,
                            {"PATH": str(binary_dir) + os.pathsep + os.environ["PATH"]},
                        ),
                        mock.patch.object(_proc, "REAP_DEADLINE_SECONDS", 0.4),
                        mock.patch.object(
                            _proc.subprocess, "Popen", side_effect=launch
                        ),
                        mock.patch.object(_proc.os, "killpg", side_effect=signal_group),
                        mock.patch.object(codex, "run_reaped", side_effect=bounded_run),
                    ):
                        step = self.start()
                        action_id = step.work_items[0].action_id
                        while (
                            step.type == "work_batch"
                            and step.work_items[0].kind == "implementation"
                        ):
                            step = build.execute_build_action(
                                step.ref, step.work_items[0].action_id
                            )
                    self.assertTrue(escalations)
                    self.assertTrue(
                        all(escalations),
                        "KILL must target an exited but unreaped owned leader",
                    )
                    child_pid = json.loads(pidfile.read_bytes())["child"]
                    state = self.state()["bl_workflow"]
                    if suppress_kill:
                        self.assertTrue(alive(child_pid))
                        self.assertEqual(len(processes), 1)
                        self.assertFalse(state["accepted_actions"])
                        self.assertEqual(state["outstanding_writer"], action_id)
                        self.assertEqual(state["action"]["status"], "claimed")
                        self.assertEqual(step.type, "waiting")
                        self.assertIsNone(step.question)
                        self.assertIn("quiescence", step.display)
                        self.assertIn("build-recover", step.display)
                        with self.assertRaisesRegex(
                            review.WorkflowError, "current issued question"
                        ):
                            build.decide_build(
                                build.BuildDecision(
                                    step.ref,
                                    "0" * 64,
                                    "retry_executor",
                                    confirmation="stack_edits",
                                    workspace_sha256="0" * 64,
                                )
                            )
                        build.cancel_build(step.ref)
                        with self.assertRaisesRegex(
                            loop_state.LoopStateError, "Outstanding writer"
                        ):
                            self.start()
                    else:
                        self.assertFalse(alive(child_pid))
                        self.assertEqual(len(processes), 1 if mode == "success" else 2)
                        expected = (
                            "completed"
                            if mode == "success"
                            else "timeout"
                            if mode == "timeout"
                            else "failed"
                        )
                        self.assertEqual(
                            [r["status"] for r in state["accepted_actions"]],
                            [expected] * len(processes),
                        )
                        if mode == "success":
                            self.assertEqual(step.work_items[0].kind, "reviewer")
                            self.assertEqual(
                                (
                                    build._root(step.ref)
                                    / "returns"
                                    / f"{action_id}.txt"
                                ).read_bytes(),
                                COMPLETED,
                            )
                        self.assertIsNone(state["outstanding_writer"])
                    self.assertEqual(
                        state["review_generation"],
                        1 if mode == "success" and not suppress_kill else 0,
                    )
                finally:
                    stop.write_text("stop fixture descendant")
                    for proc in processes:
                        proc.wait(timeout=3)
                    if pidfile.exists():
                        child_pid = json.loads(pidfile.read_bytes())["child"]
                        end = time.monotonic() + 3
                        while alive(child_pid) and time.monotonic() < end:
                            time.sleep(0.02)
                        self.assertFalse(
                            alive(child_pid), "fixture descendant must be gone"
                        )
                if suppress_kill:
                    build.recover_build_action(step.ref, action_id, "not_running")
                    self.assertIsNone(self.state()["bl_workflow"]["outstanding_writer"])
                    self.assertFalse(self.state()["active"])

    def test_chained_cursor_final_report_uses_lf_jsonl_framing(self) -> None:
        from multiagent.providers import cursor

        conversation = "12345678-1234-1234-1234-123456789abc"

        def stream(content: str) -> bytes:
            return "".join(
                json.dumps(event, ensure_ascii=False) + "\n"
                for event in (
                    {"type": "system", "subtype": "init", "session_id": conversation},
                    {
                        "type": "assistant",
                        "session_id": conversation,
                        "text": "CREW_BUILD_STATUS: COMPLETED",
                    },
                    {
                        "type": "result",
                        "subtype": "success",
                        "is_error": False,
                        "session_id": conversation,
                        "result": content,
                    },
                )
            ).encode()

        for index, (content, expected) in enumerate(
            (
                (
                    "Details\u2028remain in one JSON string\nCREW_BUILD_STATUS: BLOCKED",
                    "blocked",
                ),
                ("CREW_BUILD_STATUS: COMPLETED\u2028", "invalid_report"),
                (
                    "Details\u0085and\u2029separators\nCREW_BUILD_STATUS: BLOCKED",
                    "blocked",
                ),
            )
        ):
            with self.subTest(report=index):
                self.request = build.BuildRequest(
                    "--executor cursor-auto --seats opus task", f"cursor-jsonl-{index}"
                )
                provider = build.get_provider("cursor-auto")
                with (
                    mock.patch.object(build, "get_provider", return_value=provider),
                    mock.patch.object(
                        provider, "is_available", return_value=(True, "")
                    ),
                    mock.patch.object(
                        provider, "_supports_continuation_runtime", return_value=True
                    ),
                    mock.patch.object(
                        provider, "_supports_stream_json_runtime", return_value=True
                    ),
                    mock.patch.object(
                        cursor,
                        "run_reaped",
                        side_effect=[
                            (0, stream(COMPLETED.decode()), b""),
                            (0, stream(content), b""),
                        ],
                    ) as runner,
                ):
                    step = self.start()
                    step = build.execute_build_action(
                        step.ref, step.work_items[0].action_id
                    )
                    step = self.panel(step, "REVISE")
                    action = step.work_items[0].action_id
                    step = build.execute_build_action(step.ref, action)
                self.assertIn("--resume", runner.call_args.args[0])
                self.assertIn(conversation, runner.call_args.args[0])
                self.assertEqual(
                    self.state()["bl_workflow"]["accepted_actions"][-1]["status"],
                    expected,
                )
                self.assertEqual(
                    (build._root(step.ref) / "returns" / f"{action}.txt").read_bytes(),
                    content.encode(),
                )
                self.assertEqual(self.state()["bl_workflow"]["review_generation"], 1)
                self.assertEqual(step.type, "needs_input")

    def test_cursor_structured_report_requires_one_complete_final_response(
        self,
    ) -> None:
        from multiagent.providers import ProviderContinuation, cursor

        conversation = "12345678-1234-1234-1234-123456789abc"

        def record(value: dict) -> bytes:
            return json.dumps(value).encode() + b"\n"

        init = record({"type": "system", "subtype": "init", "session_id": conversation})
        assistant = record(
            {
                "type": "assistant",
                "session_id": conversation,
                "text": "CREW_BUILD_STATUS: COMPLETED",
            }
        )
        final = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "session_id": conversation,
            "result": COMPLETED.decode(),
        }
        valid = init + assistant + record(final)
        bad = {
            "truncated": init
            + assistant
            + record({**final, "result": "CREW_BUILD_STATUS: BLOCKED"})[:-5],
            "missing": init + assistant,
            "failed": init
            + assistant
            + record({**final, "subtype": "error", "is_error": True}),
            "no-report": init
            + assistant
            + record({k: v for k, v in final.items() if k != "result"}),
            "ambiguous-report": init
            + assistant
            + record({**final, "result": {"text": COMPLETED.decode()}}),
            "duplicate": valid + record(final),
            "conflicting": valid
            + record({**final, "result": "CREW_BUILD_STATUS: BLOCKED"}),
            "trailing-malformed": valid
            + b'{"type":"result",\nCREW_BUILD_STATUS: COMPLETED',
            "trailing-assistant": valid + assistant,
            "duplicate-key": init
            + assistant
            + record(final).replace(
                b'"result": "Implemented',
                b'"result": "CREW_BUILD_STATUS: BLOCKED", "result": "Implemented',
            ),
            "foreign-id": init
            + assistant
            + record({**final, "session_id": "foreign-session"}),
            "resume-mismatch": record(
                {"type": "system", "subtype": "init", "session_id": "foreign-session"}
            )
            + record({**final, "session_id": "foreign-session"}),
            "resume-plain": COMPLETED,
        }
        for name, raw in bad.items():
            with self.subTest(name=name):
                resumed = name in {"truncated", "resume-mismatch", "resume-plain"}
                self.request = build.BuildRequest(
                    "--executor cursor-auto --seats opus task",
                    f"cursor-authority-{name}",
                )
                provider = build.get_provider("cursor-auto")
                with (
                    mock.patch.object(build, "get_provider", return_value=provider),
                    mock.patch.object(
                        provider, "is_available", return_value=(True, "")
                    ),
                    mock.patch.object(
                        provider, "_supports_continuation_runtime", return_value=True
                    ),
                    mock.patch.object(config, "build_executor_retries", return_value=2),
                    mock.patch.object(
                        cursor, "run_reaped", return_value=(0, raw, b"")
                    ) as runner,
                ):
                    returned = provider.run(
                        "fixture",
                        sandbox="workspace-write",
                        continuation=ProviderContinuation(
                            conversation if resumed else None
                        ),
                    )
                    self.assertFalse(returned.report_authoritative)
                    self.assertEqual(returned.exact_output, raw)
                    self.assertNotIn("report_authoritative", returned.to_dict())
                    self.assertEqual(returned.transport_status, "ok")
                    step = self.start()
                    if resumed:
                        with mock.patch.object(
                            cursor, "run_reaped", return_value=(0, valid, b"")
                        ):
                            step = build.execute_build_action(
                                step.ref, step.work_items[0].action_id
                            )
                        step = self.panel(step, "REVISE")
                    generation = self.state()["bl_workflow"]["review_generation"]
                    action = step.work_items[0].action_id
                    calls = runner.call_count
                    if name == "trailing-malformed":
                        self.assertEqual(build.report_status(raw), "completed")
                        with (
                            mock.patch.object(
                                build,
                                "_accept_result",
                                side_effect=OSError("crash before state acceptance"),
                            ),
                            self.assertRaises(OSError),
                        ):
                            build.execute_build_action(step.ref, action)
                        self.assertEqual(
                            self.state()["bl_workflow"]["outstanding_writer"], action
                        )
                        step = build.next_build(step.ref)
                    else:
                        step = build.execute_build_action(step.ref, action)
                    self.assertEqual(runner.call_count, calls + 1)
                    self.assertEqual("--resume" in runner.call_args.args[0], resumed)
                    state = self.state()["bl_workflow"]
                    self.assertEqual(step.question.kind, "executor_report_recovery")
                    self.assertEqual(
                        state["accepted_actions"][-1]["status"], "invalid_report"
                    )
                    self.assertEqual(state["review_generation"], generation)
                    self.assertEqual(state["round_attempt"], 1)
                    root = build._root(step.ref)
                    self.assertEqual(
                        (root / "returns" / f"{action}.txt").read_bytes(), raw
                    )
                    receipt = json.loads(
                        (root / "execution-receipts" / f"{action}.json").read_bytes()
                    )
                    self.assertFalse(receipt["report_authoritative"])
                    self.assertEqual(receipt["transport_status"], "ok")
                    self.assertEqual(build.next_build(step.ref), step)
                    self.assertEqual(runner.call_count, calls + 1)

    def test_cursor_malformed_stream_preserves_bytes_before_build_status(self) -> None:
        from multiagent.providers import ProviderContinuation, cursor

        conversation = "12345678-1234-1234-1234-123456789abc"

        def stream(report: bytes) -> bytes:
            return (
                json.dumps(
                    {"type": "system", "subtype": "init", "session_id": conversation}
                ).encode()
                + b"\n"
                + json.dumps(
                    {
                        "type": "assistant",
                        "session_id": conversation,
                        "text": "CREW_BUILD_STATUS: COMPLETED",
                    }
                ).encode()
                + b"\n"
                + json.dumps(
                    {
                        "type": "result",
                        "subtype": "success",
                        "is_error": False,
                        "session_id": conversation,
                        "result": "REPORT",
                    }
                )
                .encode()
                .replace(b"REPORT", report)
                + b"\n"
            )

        valid = stream(b"Implemented\\nCREW_BUILD_STATUS: COMPLETED")
        malformed = (
            stream(b"Malformed \xff\\nCREW_BUILD_STATUS: COMPLETED"),
            b"Malformed transport \xff\n" + valid,
        )
        for resumed in (False, True):
            for index, raw in enumerate(malformed):
                with self.subTest(resumed=resumed, malformed=index):
                    self.request = build.BuildRequest(
                        "--executor cursor-auto --seats opus task",
                        f"cursor-invalid-{resumed}-{index}",
                    )
                    provider = build.get_provider("cursor-auto")
                    with (
                        mock.patch.object(build, "get_provider", return_value=provider),
                        mock.patch.object(
                            provider, "is_available", return_value=(True, "")
                        ),
                        mock.patch.object(
                            provider,
                            "_supports_continuation_runtime",
                            return_value=True,
                        ),
                        mock.patch.object(
                            cursor, "run_reaped", return_value=(0, raw, b"")
                        ) as runner,
                    ):
                        returned = provider.run(
                            "fixture",
                            sandbox="workspace-write",
                            workspace=str(self.root),
                            continuation=ProviderContinuation(
                                conversation if resumed else None
                            ),
                        )
                        self.assertTrue(returned.ok)
                        self.assertEqual(returned.exact_output, raw)
                        self.assertIn("CREW_BUILD_STATUS: COMPLETED", returned.output)
                        json.dumps(returned.to_dict(), ensure_ascii=False).encode(
                            "utf-8"
                        )
                        step = self.start()
                        if resumed:
                            with mock.patch.object(
                                cursor, "run_reaped", return_value=(0, valid, b"")
                            ):
                                step = build.execute_build_action(
                                    step.ref, step.work_items[0].action_id
                                )
                            step = self.panel(step, "REVISE")
                        generation = self.state()["bl_workflow"]["review_generation"]
                        action = step.work_items[0].action_id
                        with mock.patch.object(
                            review,
                            "execute_external_review",
                            side_effect=AssertionError("unexpected paid review"),
                        ):
                            step = build.execute_build_action(step.ref, action)
                    self.assertEqual("--resume" in runner.call_args.args[0], resumed)
                    state = self.state()["bl_workflow"]
                    self.assertEqual(
                        state["accepted_actions"][-1]["status"], "invalid_report"
                    )
                    self.assertEqual(
                        state["accepted_actions"][-1]["returned_sha256"],
                        build.sha256(raw),
                    )
                    self.assertEqual(state["review_generation"], generation)
                    self.assertEqual(state["round_attempt"], 1)
                    self.assertEqual(step.question.kind, "executor_report_recovery")
                    self.assertIsNone(state["outstanding_writer"])
                    original = build._root(step.ref) / "returns" / f"{action}.txt"
                    self.assertEqual(original.read_bytes(), raw)
                    self.assertEqual(build.next_build(step.ref), step)
                    retry = self.decision(
                        step,
                        "retry_executor",
                        confirmation="stack_edits",
                        workspace_sha256=step.question.workspace_sha256,
                    )
                    self.assertEqual(retry.type, "work_batch")
                    self.assertIn(
                        "INVALID UTF-8 EXECUTOR REPORT",
                        Path(
                            self.state()["bl_workflow"]["feedback_paths"][-1]
                        ).read_text(),
                    )
                    self.assertEqual(original.read_bytes(), raw)

    def test_build_capture_cli_rejects_unissued_files_before_reading(self) -> None:
        step = self.start()
        item = step.work_items[0]
        issued = Path(item.returned_path)
        issued.parent.mkdir(parents=True)
        issued.write_bytes(COMPLETED)
        unrelated = self.home / "unrelated.txt"
        unrelated.write_bytes(COMPLETED)
        owned_wrong = issued.parent / "other.txt"
        owned_wrong.write_bytes(COMPLETED)
        directory = issued.parent / "directory"
        directory.mkdir()
        fifo = issued.parent / "fifo"
        os.mkfifo(fifo)
        link = issued.parent / "alias.txt"
        link.symlink_to(unrelated)
        state_path = loop_state.resolve("bl", self.request.session_id)
        read_bytes = Path.read_bytes

        def reject(argv: list[str], forbidden: tuple[Path, ...]) -> None:
            before = state_path.read_bytes(), state_path.stat().st_mtime_ns
            retained = {
                str(p): p.read_bytes()
                for p in build._root(step.ref).rglob("*")
                if p.is_file() and not p.is_symlink()
            }
            reads: list[Path] = []

            def guarded_read(path: Path) -> bytes:
                reads.append(path)
                if path in forbidden:
                    raise AssertionError(f"unauthorized ingress read: {path}")
                return read_bytes(path)

            output = io.StringIO()
            with (
                mock.patch.object(Path, "read_bytes", guarded_read),
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(cli.main(argv), 2)
            response = json.loads(output.getvalue())
            self.assertEqual(response["schema"], 1)
            self.assertNotIn("Traceback", output.getvalue())
            self.assertFalse(set(forbidden).intersection(reads))
            self.assertEqual(
                (state_path.read_bytes(), state_path.stat().st_mtime_ns), before
            )
            self.assertEqual(
                {
                    str(p): p.read_bytes()
                    for p in build._root(step.ref).rglob("*")
                    if p.is_file() and not p.is_symlink()
                },
                retained,
            )

        base = list(item.commands["capture"][1:])
        reject(
            base, (issued,)
        )  # An issued path alone does not authorize an unclaimed action.
        build.claim_build_action(step.ref, item.action_id)
        for wrong in (
            unrelated,
            owned_wrong,
            directory,
            fifo,
            link,
            issued.parent / "missing.txt",
            issued.parent / ".." / "host-return" / issued.name,
        ):
            argv = base.copy()
            argv[argv.index("--return-file") + 1] = str(wrong)
            reject(argv, (wrong, unrelated))
        issued.unlink()
        issued.symlink_to(unrelated)
        reject(base, (issued, unrelated))
        issued.unlink()
        issued.mkdir()
        reject(base, (issued,))
        issued.rmdir()
        issued.write_bytes(COMPLETED)
        retained_parent = issued.parent.with_name("saved-host-return")
        issued.parent.rename(retained_parent)
        issued.parent.symlink_to(retained_parent, target_is_directory=True)
        reject(base, (issued, retained_parent / issued.name))
        issued.parent.unlink()
        retained_parent.rename(issued.parent)
        stale = base.copy()
        stale[stale.index("--action-id") + 1] = "action-9999"
        reject(stale, (issued,))
        foreign = base.copy()
        foreign[foreign.index("--loop-instance-id") + 1] = "0" * 32
        reject(foreign, (issued,))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(base), 0)
            accepted = self.state()["bl_workflow"]["accepted_actions"]
            self.assertEqual(cli.main(base), 0)
            self.assertEqual(self.state()["bl_workflow"]["accepted_actions"], accepted)
        step = self.panel(build.next_build(step.ref), "REVISE")
        reject(base, (issued,))
        self.assertEqual(step.work_items[0].action_id, "action-0002")
        build.claim_build_action(step.ref, "action-0002")
        # The in-memory host transport still accepts supplied exact bytes.
        self.assertEqual(
            transport.capture_build_return(step.ref, "action-0002", COMPLETED)
            .work_items[0]
            .kind,
            "reviewer",
        )

        self.request = build.BuildRequest(
            "--executor sol --seats opus task", "external-file-capture"
        )
        provider = mock.Mock()
        provider.is_available.return_value = (True, "")
        provider.effective_timeout.side_effect = lambda value: value
        with mock.patch.object(build, "get_provider", return_value=provider):
            step = self.start()
        external = step.work_items[0]
        build._claim(
            step.ref,
            external.action_id,
            execution.observe_workspace(str(self.root)),
            external=True,
        )
        state_path = loop_state.resolve("bl", self.request.session_id)
        argv = [
            *build._owner_argv(step.ref, "build-capture")[1:],
            "--action-id",
            external.action_id,
            "--return-file",
            str(unrelated),
        ]
        reject(argv, (unrelated,))

    def test_admission_is_free_during_chain_waits_and_baseline_probe(self) -> None:
        for operation in (
            "build-chain",
            "build-baseline",
            "measure-chain",
            "state-init-chain",
        ):
            with self.subTest(operation=operation):
                session = operation
                entered, release = threading.Event(), threading.Event()
                before = None
                if operation == "measure-chain":
                    old = build.start_build(
                        dataclasses.replace(self.request, session_id=session)
                    )
                    build.cancel_build(old.ref)
                    before = loop_state.resolve("bl", session).read_bytes()

                @contextlib.contextmanager
                def blocked_chain(*_args: object, **_kwargs: object) -> Iterator[None]:
                    entered.set()
                    if not release.wait(5):
                        raise AssertionError("barrier not released")
                    raise continuations.ContinuationLockError("fixture chain busy")
                    yield

                observe = execution.observe_workspace

                def blocked_baseline(workspace: str) -> execution.WorkspaceFacts:
                    entered.set()
                    if not release.wait(5):
                        raise AssertionError("barrier not released")
                    return observe(workspace)

                def start() -> object:
                    if operation == "measure-chain":
                        return loop_state.initialize("plan", session, journal={})
                    if operation == "state-init-chain":
                        spec = importlib.util.spec_from_file_location(
                            "fixture_crew_state",
                            Path(__file__).resolve().parents[1] / "crew-state.py",
                        )
                        module = importlib.util.module_from_spec(spec)
                        spec.loader.exec_module(module)
                        with mock.patch.object(
                            sys,
                            "argv",
                            [
                                "crew-state",
                                "init",
                                "bl",
                                "--session-id",
                                session,
                                "--prompt",
                                "task",
                            ],
                        ):
                            return module.main()
                    return build.start_build(
                        dataclasses.replace(self.request, session_id=session)
                    )

                patch = (
                    mock.patch.object(
                        execution, "observe_workspace", side_effect=blocked_baseline
                    )
                    if operation == "build-baseline"
                    else mock.patch.object(
                        continuations, "continuation_lock", side_effect=blocked_chain
                    )
                )
                with patch, concurrent.futures.ThreadPoolExecutor() as pool:
                    future = pool.submit(start)
                    try:
                        self.assertTrue(entered.wait(5))
                        with models.state_lock(
                            self.root / ".crew/loop-admission", timeout=0.15
                        ):
                            pass
                    finally:
                        release.set()
                    if operation == "measure-chain":
                        with self.assertRaisesRegex(
                            loop_state.LoopStateError, "Continuation chain is busy"
                        ):
                            future.result(5)
                        self.assertEqual(
                            loop_state.resolve("bl", session).read_bytes(), before
                        )
                        self.assertFalse(loop_state.resolve("mt", session).exists())
                    elif operation == "state-init-chain":
                        with self.assertRaises(SystemExit) as caught:
                            future.result(5)
                        self.assertEqual(caught.exception.code, 2)
                    else:
                        step = future.result(5)
                        if operation == "build-chain":
                            self.assertEqual(step.type, "waiting")
                            self.assertIsNone(step.ref)
                        else:
                            self.assertEqual(step.type, "work_batch")

    def test_overlapping_cli_starts_wait_on_chain_without_holding_admission(
        self,
    ) -> None:
        requests = self.root / ".crew/requests"
        requests.mkdir(parents=True)
        command = Path(__file__).resolve().parents[2] / "crew"
        processes: list[subprocess.Popen] = []
        with continuations.continuation_lock(self.request.session_id, "build-executor"):
            try:
                for name in ("first", "second"):
                    spill = requests / f"{name}.json"
                    spill.write_text(
                        json.dumps(
                            {"schema": 1, "raw_arguments": self.request.raw_arguments}
                        )
                    )
                    processes.append(
                        subprocess.Popen(
                            [
                                str(command),
                                "build",
                                "-f",
                                str(spill),
                                "--session-id",
                                self.request.session_id,
                                "--consume",
                            ],
                            env={**os.environ, "HOME": str(self.home)},
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            text=True,
                        )
                    )
                for proc in processes:
                    stdout, stderr = proc.communicate(timeout=12)
                    self.assertEqual(proc.returncode, 0, stderr)
                    result = json.loads(stdout)
                    self.assertEqual(result["type"], "waiting")
                    self.assertIsNone(result["ref"])
                    self.assertIn("retry the same build request", result["display"])
                    self.assertNotIn("Traceback", stderr)
            finally:
                for proc in processes:
                    if proc.poll() is None:
                        proc.kill()
                        proc.communicate(timeout=3)
        self.assertFalse(loop_state.resolve("bl", self.request.session_id).exists())
        self.assertEqual(len(list(requests.glob("*.json"))), 2)

    def test_cli_lock_timeouts_are_retryable_json_and_preserve_requests(self) -> None:
        requests = self.root / ".crew/requests"
        requests.mkdir(parents=True)
        spill = requests / "start.json"
        spill.write_text(
            json.dumps({"schema": 1, "raw_arguments": self.request.raw_arguments})
        )
        command = Path(__file__).resolve().parents[2] / "crew"
        for verb in ("build", "build-resume"):
            with self.subTest(verb=verb):
                argv = [str(command), verb, "--session-id", self.request.session_id]
                if verb == "build":
                    argv += ["-f", str(spill), "--consume"]
                    locked = self.root / ".crew/loop-admission"
                else:
                    self.start()
                    locked = loop_state.resolve("bl", self.request.session_id)
                before = locked.read_bytes() if locked.is_file() else None
                with models.state_lock(locked):
                    result = subprocess.run(
                        argv,
                        env={**os.environ, "HOME": str(self.home)},
                        capture_output=True,
                        text=True,
                        timeout=8,
                        check=False,
                    )
                self.assertEqual(result.returncode, 2)
                body = json.loads(result.stdout)
                self.assertEqual(body["schema"], 1)
                self.assertEqual(body["code"], "build_error")
                self.assertIn("retry the same command", body["message"])
                self.assertNotIn("Traceback", result.stderr)
                self.assertTrue(spill.exists())
                if before is not None:
                    self.assertEqual(locked.read_bytes(), before)

    def test_uncertain_filename_owner_protects_actual_swab(self) -> None:
        crew_dir = self.root / ".crew"
        crew_dir.mkdir()
        for owner in ("owner", "recorded", "unrelated"):
            run = crew_dir / "reviews" / owner / "run-0123456789ab"
            run.mkdir(parents=True)
            (run / "run.json").write_text("{}")
        state = crew_dir / "build-state-owner.json"
        for recorded in (None, "recorded", 42):
            with self.subTest(recorded=recorded):
                data = {"active": False, "bl_workflow": {}}
                if recorded is not None:
                    data["session_id"] = recorded
                models.atomic_write_json(state, data)
                keys = artifact_prune.live_run_keys(crew_dir)
                self.assertIn(("owner", "*"), keys)
                if recorded == "recorded":
                    self.assertIn(("recorded", "*"), keys)
                self.assertNotIn(
                    crew_dir / "reviews/owner/run-0123456789ab",
                    {
                        p.path
                        for p in artifact_prune.collect_prunable(crew_dir, time.time())
                    },
                )
        models.atomic_write_json(
            state, {"active": False, "bl_workflow": {}, "session_id": "recorded"}
        )
        before = state.read_bytes()
        with (
            contextlib.redirect_stdout(io.StringIO()) as output,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(cli.main(["swab", "--yes", "--json"]), 0)
        self.assertEqual(
            [p["path"] for p in json.loads(output.getvalue())["removed"]],
            [str(crew_dir / "reviews/unrelated/run-0123456789ab")],
        )
        self.assertEqual(state.read_bytes(), before)
        self.assertTrue((crew_dir / "reviews/owner/run-0123456789ab").exists())
        self.assertTrue((crew_dir / "reviews/recorded/run-0123456789ab").exists())

    def test_verbose_status_retains_json_and_inactive_writer_recovery(self) -> None:
        import shlex

        import loop_projection

        step = self.start()
        action = step.work_items[0].action_id
        build.claim_build_action(step.ref, action)
        data = self.state()
        data["awaiting_input"] = True
        self.assertIn(
            "bound human question", loop_projection.project_build(data).render()
        )
        self.assertIn(
            "Outstanding writer", loop_projection.project_build(data).render()
        )
        build.cancel_build(step.ref)
        state = loop_state.resolve("bl", self.request.session_id)
        before = state.read_bytes()
        result = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve().parents[1] / "crew-state.py"),
                "show",
                "bl",
                "--verbose",
                "--session-id",
                self.request.session_id,
            ],
            env={**os.environ, "HOME": str(self.home)},
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertFalse(json.loads(result.stdout)["active"])
        self.assertIn(shlex.join(build.recovery_argv(step.ref, action)), result.stderr)
        self.assertIn(
            "explicit operator confirmation of actual quiescence", result.stderr
        )
        self.assertEqual(state.read_bytes(), before)

    def test_changed_digest_force_decision_is_retained_and_replays(self) -> None:
        step = self.panel(self.draft(), failed=1)
        decision = build.BuildDecision(
            step.ref, step.question.question_id, "force", "force"
        )
        drift = self.root / "drift.txt"
        drift.write_text("changed")
        observe = build._workspace_digest

        def observe_then_restore() -> str:
            digest = observe()
            # Revalidation sees the authorized facts after this barrier.
            drift.unlink(missing_ok=True)
            return digest

        with mock.patch.object(
            build, "_workspace_digest", side_effect=observe_then_restore
        ):
            done = build.decide_build(decision)
        self.assertEqual(done.type, "terminal")
        self.assertEqual(
            self.state()["bl_workflow"]["decisions"][-1],
            build.decision_to_dict(decision),
        )
        before = self.state()
        self.assertEqual(build.decide_build(decision).outcome, done.outcome)
        self.assertEqual(self.state(), before)

    def test_review_timeout_floor_freeze_and_explicit_new_generation(self) -> None:
        self.request = dataclasses.replace(
            self.request, raw_arguments="--executor crew:executor --seats sol task"
        )

        def provider_timeout(
            seat: str, provider: str, channel: str, base: int, **kwargs: object
        ) -> int:
            self.assertTrue(kwargs.get("build"))
            return max(1200, base)

        with (
            mock.patch.object(
                review, "_provider_timeout", side_effect=provider_timeout
            ),
            contextlib.redirect_stderr(io.StringIO()) as warnings,
        ):
            step = self.draft()
        self.assertNotIn("540", warnings.getvalue())
        for item in step.work_items:
            self.assertEqual(item.timeout_seconds, 1200)
            self.assertEqual(item.host_allowance_seconds, 1260)
        first_ref = step.work_items[0].review_ref
        build.park_review_timeout(step.ref, "injected provider floor drift")
        step = build.next_build(step.ref)
        self.assertEqual(step.question.kind, "review_timeout_changed")
        with mock.patch.object(review, "_provider_timeout", return_value=1300):
            step = self.decision(
                step,
                "fresh_review",
                confirmation="fresh_review",
                workspace_sha256=step.question.workspace_sha256,
            )
        self.assertNotEqual(step.work_items[0].review_ref.run_id, first_ref.run_id)
        self.assertEqual(step.work_items[0].timeout_seconds, 1300)

    def test_prepared_checkpoint_replays_same_generation_without_probe(self) -> None:
        step = self.start()
        action = step.work_items[0].action_id
        build.claim_build_action(step.ref, action)
        with (
            mock.patch.object(
                review,
                "_start_loop_review_under_owner_lock",
                side_effect=OSError("crash after checkpoint"),
            ),
            self.assertRaises(OSError),
        ):
            build.capture_build_return(step.ref, action, COMPLETED)
        prepared = self.state()["bl_workflow"]["pending_review_inputs"]
        with mock.patch.object(
            review,
            "prepare_loop_review",
            side_effect=AssertionError("must reuse checkpoint"),
        ):
            step = build.next_build(step.ref)
        self.assertEqual(
            step.work_items[0].review_ref.run_id, prepared["ref"]["run_id"]
        )

    def test_simultaneous_build_mt_admission_admits_one(self) -> None:
        barrier = threading.Barrier(2)

        def start_bl() -> str:
            barrier.wait(5)
            try:
                self.start()
                return "bl"
            except loop_state.LoopStateError:
                return "refused"

        def start_mt() -> str:
            barrier.wait(5)
            try:
                loop_state.initialize("plan", self.request.session_id, journal={})
                return "mt"
            except loop_state.LoopStateError:
                return "refused"

        with concurrent.futures.ThreadPoolExecutor() as pool:
            a, b = pool.submit(start_bl), pool.submit(start_mt)
            results = [a.result(10), b.result(10)]
        self.assertEqual(results.count("refused"), 1)

    def test_native_runtime_launches_panel_before_wait_and_cancel_race(self) -> None:
        events = []

        class Runtime:
            def launch(self, item: build.BuildWorkItem) -> str:
                events.append(("launch", item.kind))
                return item.action_id

            def completions(self, handles: tuple[str, ...]):
                events.append(("wait", len(handles)))
                for handle in handles:
                    yield transport.Completion(
                        handle, COMPLETED if len(handles) == 1 else VALID
                    )

        step = transport.run_build_batch(self.start(), Runtime())
        events.clear()
        step = transport.run_build_batch(step, Runtime())
        self.assertEqual(
            events[:3], [("launch", "reviewer"), ("launch", "reviewer"), ("wait", 2)]
        )
        self.assertEqual(step.work_items[0].kind, "synthesis")

    def test_malformed_writer_cleanup_and_cross_session_owner(self) -> None:
        step = self.start()
        build.claim_build_action(step.ref, step.work_items[0].action_id)
        state_path = loop_state.resolve("bl", self.request.session_id)
        lock = state_path.with_name(state_path.name + ".lock")
        state_path.write_text("{broken")
        stale = time.time() - 10 * 86400
        os.utime(state_path, (stale, stale))
        os.utime(lock, (stale, stale))
        spec = importlib.util.spec_from_file_location(
            "cleanup_build_test",
            Path(__file__).resolve().parents[1] / "session-start.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.cleanup_stale_files(self.root / ".crew")
        self.assertTrue(state_path.exists())
        self.assertTrue(lock.exists())
        self.assertIn(
            (self.request.session_id, "*"),
            artifact_prune.live_run_keys(self.root / ".crew"),
        )
        with self.assertRaises((review.WorkflowError, loop_state.LoopStateError)):
            build.recover_build_action(
                dataclasses.replace(step.ref, session_segment="new-session"),
                step.work_items[0].action_id,
                "not_running",
            )

    def test_chained_executor_resume_and_admission_while_running(self) -> None:
        self.request = build.BuildRequest(
            "--executor sol --seats opus task", self.request.session_id
        )
        entered, release = threading.Event(), threading.Event()
        calls = []
        conversation = "01234567-89ab-cdef-0123-456789abcdef"

        class Provider:
            supports_continuation = True

            def is_available(self) -> tuple[bool, str]:
                return True, ""

            def run(inner, prompt: str, **options: object) -> ProviderResult:
                calls.append(options)
                if len(calls) == 1:
                    entered.set()
                    release.wait(5)
                resumed = (
                    options.get("continuation")
                    and options["continuation"].conversation_id
                )
                return ProviderResult(
                    "sol",
                    options["model"],
                    True,
                    COMPLETED.decode(),
                    None,
                    0.01,
                    continuation=ContinuationOutcome(
                        "supported", "resume" if resumed else "fresh", conversation
                    ),
                )

        with mock.patch.object(build, "get_provider", return_value=Provider()):
            step = self.start()
            with concurrent.futures.ThreadPoolExecutor() as pool:
                pending = pool.submit(
                    build.execute_build_action, step.ref, step.work_items[0].action_id
                )
                self.assertTrue(entered.wait(3))
                path = loop_state.resolve("bl", self.request.session_id)
                before = path.read_bytes()
                self.assertEqual(self.start().type, "waiting")
                self.assertEqual(
                    build.resume_build(self.request.session_id).type, "waiting"
                )
                with self.assertRaises(review.WorkflowError):
                    build.start_build(
                        dataclasses.replace(
                            self.request,
                            raw_arguments=self.request.raw_arguments + " changed",
                        )
                    )
                self.assertEqual(path.read_bytes(), before)
                release.set()
                step = pending.result(5)
            step = self.panel(step, "REVISE")
            step = build.execute_build_action(step.ref, step.work_items[0].action_id)
        self.assertEqual(calls[1]["continuation"].conversation_id, conversation)
        self.assertEqual(
            continuations.load_record(
                self.request.session_id, "build-executor"
            ).conversation_id,
            conversation,
        )

    def test_public_cli_decision_and_hook_argv(self) -> None:
        step = self.draft(b"Need format\nCREW_BUILD_STATUS: BLOCKED")
        answer = self.root / "answer.txt"
        answer.write_text("Use the requested format")
        # Freeze the decision observation after creating a spill outside reviewed content.
        answer = self.root / ".crew" / "answer.txt"
        (self.root / "answer.txt").unlink()
        answer.write_text("Use the requested format")
        argv = [
            "build-decide",
            "--session-segment",
            step.ref.session_segment,
            "--loop-instance-id",
            step.ref.loop_instance_id,
            "--question-id",
            step.question.question_id,
            "--kind",
            "answer_executor",
            "--answer-file",
            str(answer),
            "--confirmation",
            "stack_edits",
            "--workspace-sha256",
            step.question.workspace_sha256,
        ]
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(argv), 0)
        issued = json.loads(out.getvalue())
        self.assertEqual(issued["type"], "work_batch")
        script = Path(__file__).resolve().parents[1] / "persistent-mode.py"
        env = dict(os.environ, HOME=str(self.home))
        completed = subprocess.run(
            [sys.executable, str(script)],
            input=json.dumps(
                {"cwd": str(self.root), "session_id": self.request.session_id}
            ),
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        body = json.loads(completed.stdout)["reason"]
        self.assertIn("build-next", body)
        self.assertNotIn("review-prep", body)
        import shlex

        line = next(line for line in body.split("\n") if line.startswith("Next: "))
        next_argv = shlex.split(line.removeprefix("Next: "))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(next_argv[1:]), 0)

    def test_cancel_cli_and_state_mutation_ownership(self) -> None:
        step = self.start()
        state_path = loop_state.resolve("bl", self.request.session_id)
        before = state_path.read_bytes()
        state_cli = Path(__file__).resolve().parents[1] / "crew-state.py"
        for args in (
            ["set", "bl", "--field", "phase", "--value", "done"],
            ["deactivate", "bl", "--cancel"],
            ["init", "bl", "--prompt", "overwrite"],
        ):
            result = subprocess.run(
                [
                    sys.executable,
                    str(state_cli),
                    *args,
                    "--session-id",
                    self.request.session_id,
                ],
                cwd=self.root,
                env=dict(os.environ, HOME=str(self.home)),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(state_path.read_bytes(), before)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                cli.main(list(build._owner_argv(step.ref, "build-cancel"))[1:]), 0
            )
        self.assertFalse(self.state()["active"])

    def test_review_snapshot_pointer_independence_and_external_cursor(self) -> None:
        self.request = dataclasses.replace(
            self.request,
            raw_arguments="--executor crew:executor --seats cursor-auto,sol task",
        )
        (self.root / "tracked-staged.txt").write_text("tracked test content")
        step = self.draft()
        for item in step.work_items:
            self.assertEqual(item.driver, "external")
            self.assertNotIn("--chain", item.commands["execute"])
        run = review._guard_review_path(
            session_segment=step.ref.session_segment,
            run_id=step.work_items[0].review_ref.run_id,
            create=False,
        )
        record = json.loads((run / "run.json").read_bytes())
        snapshot = (run / record["snapshot"]).read_text()
        self.assertIn("tracked test content", snapshot)
        self.assertIn("hello", snapshot)
        artifact = record["workflow_identity"]["executor_report"]
        self.assertEqual(Path(artifact["path"]).read_bytes(), COMPLETED)
        self.assertEqual(artifact["sha256"], build.sha256(COMPLETED))
        self.assertNotIn(
            COMPLETED.decode(), record["workflow_identity"]["executor_summary"]
        )
        (run.parent / "current-run").write_text("foreign")
        self.assertEqual(build.next_build(step.ref).work_items, step.work_items)

    def test_cleanup_rechecks_claim_under_lock(self) -> None:
        step = self.start()
        state_path = loop_state.resolve("bl", self.request.session_id)
        old = time.time() - 10 * 86400
        os.utime(state_path, (old, old))
        spec = importlib.util.spec_from_file_location(
            "cleanup_race_test",
            Path(__file__).resolve().parents[1] / "session-start.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        original = module.state_lock

        @contextlib.contextmanager
        def raced(path: Path):
            build.claim_build_action(step.ref, step.work_items[0].action_id)
            with original(path):
                yield

        with mock.patch.object(module, "state_lock", side_effect=raced):
            module.cleanup_stale_files(self.root / ".crew")
        self.assertTrue(state_path.exists())
        self.assertEqual(build.next_build(step.ref).type, "waiting")

    def test_cross_session_banner_prints_exact_recovery_without_adoption(self) -> None:
        step = self.start()
        action = step.work_items[0].action_id
        build.claim_build_action(step.ref, action)
        build.cancel_build(step.ref)
        spec = importlib.util.spec_from_file_location(
            "banner_build_test",
            Path(__file__).resolve().parents[1] / "session-start.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        messages = "\n".join(
            module.build_session_status(self.root, self.home, "new-owner")
        )
        self.assertIn("--session-segment " + step.ref.session_segment, messages)
        self.assertIn("--loop-instance-id " + step.ref.loop_instance_id, messages)
        self.assertIn("--action-id " + action, messages)
        self.assertIn("--confirmation not_running", messages)
        with self.assertRaises((review.WorkflowError, loop_state.LoopStateError)):
            build.resume_build("new-owner")
        self.assertEqual(self.state()["bl_workflow"]["outstanding_writer"], action)

    def test_all_decision_verbs_through_public_cli(self) -> None:
        def answer_cli(step: build.BuildStep, kind: str, **fields: str) -> dict:
            argv = list(build._owner_argv(step.ref, "build-decide"))[1:] + [
                "--question-id",
                step.question.question_id,
                "--kind",
                kind,
            ]
            for key, value in fields.items():
                argv.extend(["--" + key.replace("_", "-"), value])
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(cli.main(argv), 0)
            return json.loads(output.getvalue())

        for kind in ("retry_executor", "adopt_edits", "answer_executor", "cancel"):
            self.request = dataclasses.replace(self.request, session_id="cli-" + kind)
            step = self.draft(b"Needs guidance\nCREW_BUILD_STATUS: BLOCKED")
            fields = {}
            if kind != "cancel":
                fields = {
                    "confirmation": "completed"
                    if kind == "adopt_edits"
                    else "stack_edits",
                    "workspace_sha256": step.question.workspace_sha256,
                }
            if kind == "adopt_edits":
                fields["completed_action"] = step.question.action_id
            if kind == "answer_executor":
                answer = self.root / ".crew" / "operator-answer.txt"
                answer.write_text("Use the requested format")
                fields["answer_file"] = str(answer)
            self.assertIn(
                answer_cli(step, kind, **fields)["type"], {"work_batch", "terminal"}
            )
        for kind in ("force", "retry_review"):
            self.request = dataclasses.replace(self.request, session_id="cli-" + kind)
            step = self.panel(self.draft(), failed=1)
            self.assertEqual(step.question.kind, "completion_advisory")
            result = answer_cli(
                step, kind, **({"confirmation": "force"} if kind == "force" else {})
            )
            self.assertIn(result["type"], {"terminal", "work_batch"})
        self.request = dataclasses.replace(self.request, session_id="cli-synthesis")
        step = self.panel(self.draft(), synthesis_fail=True)
        self.assertEqual(
            answer_cli(step, "retry_synthesis")["work_items"][0]["kind"], "synthesis"
        )
        self.request = dataclasses.replace(self.request, session_id="cli-floor")
        step = self.draft()
        build.park_review_timeout(step.ref, "injected floor drift")
        step = build.next_build(step.ref)
        self.assertEqual(
            answer_cli(
                step,
                "fresh_review",
                confirmation="fresh_review",
                workspace_sha256=step.question.workspace_sha256,
            )["type"],
            "work_batch",
        )
        self.request = dataclasses.replace(self.request, session_id="cli-route")
        step = self.start()
        with mock.patch.object(build, "_route_matches", return_value=False):
            step = build.next_build(step.ref)
        self.assertEqual(answer_cli(step, "retry_route")["type"], "work_batch")
        self.request = dataclasses.replace(self.request, session_id="cli-guard")
        step = self.start()
        baseline = execution.observe_workspace(str(self.root))
        action = step.work_items[0].action_id
        build.claim_build_action(step.ref, action)
        subprocess.run(
            ["git", "symbolic-ref", "HEAD", "refs/heads/guard"],
            cwd=self.root,
            check=True,
        )
        step = build.capture_build_return(step.ref, action, COMPLETED)
        subprocess.run(
            ["git", "symbolic-ref", "HEAD", "refs/heads/" + baseline.branch],
            cwd=self.root,
            check=True,
        )
        self.assertEqual(answer_cli(step, "recheck_workspace")["type"], "work_batch")
        self.request = dataclasses.replace(self.request, session_id="cli-feedback")
        step = self.start()
        action = step.work_items[0].action_id
        build.claim_build_action(step.ref, action)
        with (
            mock.patch.object(
                review,
                "prepare_loop_review",
                side_effect=OSError("injected before review"),
            ),
            self.assertRaises(OSError),
        ):
            build.capture_build_return(step.ref, action, COMPLETED)
        returned = build._root(step.ref) / "returns" / (action + ".txt")
        saved = returned.read_bytes()
        returned.write_bytes(b"\xff")
        step = build.next_build(step.ref)
        self.assertEqual(step.question.kind, "feedback_recovery")
        returned.write_bytes(saved)
        self.assertEqual(answer_cli(step, "retry_feedback")["type"], "work_batch")

    def test_journal_refuses_tampered_immutable_request(self) -> None:
        self.start()
        value = self.state()["bl_workflow"]
        value["selection"]["task"] = "different task"
        with self.assertRaises(review.WorkflowError):
            build.journal_from_dict(value)


if __name__ == "__main__":
    unittest.main()
