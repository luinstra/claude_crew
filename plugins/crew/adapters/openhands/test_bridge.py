"""Real Crew engine plus native SDK agents; only provider responses are fixtures."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_sdk_contract import LLM, MODELS, fixture_response

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from bridge import CrewBridge
from multiagent import build_workflow as build
from multiagent import channels, config
from multiagent import measure_twice as measure
from multiagent import review_workflow as review
from multiagent.native_binding import NativeLaunch, bind_native_launch
from runtime import LaunchRefused, OpenHandsRuntime, RuntimeConflict


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.env = patch.dict(
            os.environ, {"CREW_HOST": "openhands", "CLAUDE_PROJECT_DIR": str(self.root)}
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        config._reset_cache_for_tests()
        config._global_cache = {}
        config._cache = {
            "seats": {
                "oh-one": {"via": ["openhands"], "model": MODELS[0]},
                "oh-two": {"via": ["openhands"], "model": MODELS[1]},
            }
        }
        config._cache["openhands"] = {
            "advisor_model": MODELS[0],
            "executor_model": MODELS[0],
        }
        self.addCleanup(config._reset_cache_for_tests)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.plan = self.root / "plan.md"
        self.plan.write_text("# Plan\nWrite a greeting file.\n")
        self.models = {
            model: LLM(
                model=model,
                api_key="offline",
                max_input_tokens=32768,
                max_output_tokens=1024,
            )
            for model in MODELS
        }
        self.runtime = OpenHandsRuntime(
            self.root / ".crew" / "sdk", self.models, max_concurrency=2
        )
        self.bridge = CrewBridge(self.runtime, support_model=MODELS[0])

    async def test_native_panel_and_synthesis_reach_engine_terminal(self) -> None:
        calls = []

        async def complete(llm: LLM, **kwargs: object):
            text = str(kwargs.get("messages"))
            calls.append(llm.model)
            result = (
                'Complete synthesis\nCREW_JUDGMENT: {"verdict":"APPROVED","minor_only":false}'
                if "CREW_JUDGMENT" in text
                else "## VERDICT\nAPPROVED\n\n## FINDINGS\nnone\n"
            )
            return fixture_response("finish", {"message": result})

        step = review.start_review(
            review.ReviewRequest(
                str(self.plan), seats="oh-one,oh-two", session_id="oh-review"
            )
        )
        self.assertTrue(
            all(
                item.channel == "openhands" and item.driver == "native"
                for item in step.work_items
            )
        )
        self.assertTrue(
            all(
                item.return_transport["primary"]["kind"] == "host_write"
                for item in step.work_items
            )
        )
        with patch.object(LLM, "agenerate", complete):
            result = await self.bridge.run_review(step)
        self.assertEqual(result.type, review.StepType.TERMINAL, result)
        self.assertEqual(result.outcome["judgment"], "APPROVED")
        self.assertEqual(len(calls), 3)

    async def test_debate_keeps_null_judgment(self) -> None:
        async def complete(llm: LLM, **kwargs: object):
            return fixture_response(
                "finish",
                {"message": "Consider the simpler implementation and measure it."},
            )

        step = review.start_debate(
            review.DebateRequest(
                "Choose a design",
                seats="oh-one,oh-two",
                session_id="oh-debate",
                rounds=2,
            )
        )
        with patch.object(LLM, "agenerate", complete):
            result = await self.bridge.run_review(step, discuss=True)
        self.assertEqual(result.type, review.StepType.TERMINAL, result)
        self.assertIsNone(result.outcome["judgment"])

    async def test_measure_twice_drafts_and_reviews_natively(self) -> None:
        async def complete(llm: LLM, **kwargs: object):
            text = str(kwargs.get("messages"))
            if "Crew adapter role: advisor" in text:
                if "File written" not in text:
                    job = [
                        job
                        for job in self.runtime.jobs.values()
                        if job.action.role == "advisor"
                    ][-1]
                    return fixture_response(
                        "crew_write",
                        {
                            "path": job.action.staging_path,
                            "content": "# Plan\nCreate hello.txt and verify its contents.",
                        },
                    )
                report = "Plan written"
            elif "CREW_JUDGMENT" in text:
                report = 'Complete synthesis\nBLOCKING_CAUSES: 0\nCREW_JUDGMENT: {"verdict":"APPROVED","minor_only":false}'
            else:
                report = "## VERDICT\nAPPROVED\n\n## FINDINGS\nnone\n"
            return fixture_response("finish", {"message": report})

        step = measure.start_measure_twice(
            measure.MeasureRequest(f"--seats oh-one,oh-two {self.plan}", "oh-plan")
        )
        with patch.object(LLM, "agenerate", complete):
            result = await self.bridge.run_measure(step)
        self.assertEqual(result.type, review.StepType.TERMINAL, result)
        self.assertEqual(result.outcome["status"], "approved")

    async def test_build_implements_and_reviews_natively(self) -> None:
        async def complete(llm: LLM, **kwargs: object):
            text = str(kwargs.get("messages"))
            if "Crew adapter role: executor" in text:
                if "File written" not in text:
                    return fixture_response(
                        "crew_write", {"path": "hello.txt", "content": "hello"}
                    )
                report = "Implemented\nCREW_BUILD_STATUS: COMPLETED"
            elif "CREW_JUDGMENT" in text:
                report = 'Complete synthesis\nBLOCKING_CAUSES: 0\nCREW_JUDGMENT: {"verdict":"APPROVED","minor_only":false}'
            else:
                report = "## VERDICT\nAPPROVED\n\n## FINDINGS\nnone\n"
            return fixture_response("finish", {"message": report})

        step = build.start_build(
            build.BuildRequest(
                "--seats oh-one,oh-two --executor crew:executor Write hello.txt",
                "oh-build",
            )
        )
        with patch.object(LLM, "agenerate", complete):
            result = await self.bridge.run_build(step)
        self.assertEqual(result.type, review.StepType.TERMINAL, result)
        self.assertEqual((self.root / "hello.txt").read_text(), "hello")
        self.assertEqual(result.outcome["status"], "approved")

    async def test_build_revision_uses_new_owned_executor_and_review_actions(
        self,
    ) -> None:
        rounds = 0

        async def complete(llm: LLM, **kwargs: object):
            nonlocal rounds
            text = str(kwargs.get("messages"))
            if "Crew adapter role: executor" in text:
                report = "Implemented\nCREW_BUILD_STATUS: COMPLETED"
            elif "CREW_JUDGMENT" in text:
                verdict = "REVISE" if rounds == 0 else "APPROVED"
                rounds += 1
                report = f'Summary\nBLOCKING_CAUSES: {1 if verdict == "REVISE" else 0}\nCREW_JUDGMENT: {{"verdict":"{verdict}","minor_only":false}}'
            else:
                verdict = "REVISE" if rounds == 0 else "APPROVED"
                report = f"## VERDICT\n{verdict}\n\n## FINDINGS\nnone\n"
            return fixture_response("finish", {"message": report})

        step = build.start_build(
            build.BuildRequest(
                "--seats oh-one --executor crew:executor Write hello.txt", "oh-revision"
            )
        )
        with patch.object(LLM, "agenerate", complete):
            result = await self.bridge.run_build(step)
        self.assertEqual(result.outcome["status"], "approved")
        self.assertEqual(rounds, 2)
        writers = [
            job for job in self.runtime.jobs.values() if job.action.role == "executor"
        ]
        self.assertEqual(len(writers), 2)
        self.assertNotEqual(writers[0].action.action_id, writers[1].action.action_id)
        self.assertNotEqual(writers[0].handle, writers[1].handle)

    async def test_missing_sdk_route_does_not_claim(self) -> None:
        step = review.start_review(
            review.ReviewRequest(
                str(self.plan), seats="oh-one,oh-two", session_id="oh-missing"
            )
        )
        del self.runtime.models[MODELS[1]]
        with self.assertRaises(ValueError):
            await self.bridge.review_batch(step)
        unchanged = review.next_review(step.ref)
        self.assertEqual(unchanged.in_flight, ())
        self.assertEqual(len(unchanged.work_items), 2)

    async def test_review_reconciles_completed_sdk_without_new_calls(self) -> None:
        calls = []

        async def complete(llm: LLM, **kwargs: object):
            calls.append(llm.model)
            text = str(kwargs.get("messages"))
            report = (
                'Synthesis\nCREW_JUDGMENT: {"verdict":"APPROVED","minor_only":false}'
                if "CREW_JUDGMENT" in text
                else "## VERDICT\nAPPROVED\n\n## FINDINGS\nnone\n"
            )
            return fixture_response("finish", {"message": report})

        step = review.start_review(
            review.ReviewRequest(
                str(self.plan), seats="oh-one,oh-two", session_id="oh-recover"
            )
        )
        with patch.object(LLM, "agenerate", complete):
            handles = []
            for item in step.work_items:
                review.claim_review_action(
                    review.ClaimRequest(step.ref, item.action_id)
                )
                handle = self.runtime.launch(
                    self.bridge._action(step.ref, item, discuss=False)
                )
                bind_native_launch(
                    NativeLaunch(step.ref, item.action_id, handle), channel="openhands"
                )
                handles.append(handle)
            _ = [result async for result in self.runtime.completions(tuple(handles))]
            restarted = OpenHandsRuntime(
                self.runtime.root, self.models, max_concurrency=2
            )
            bridge = CrewBridge(restarted, support_model=MODELS[0])
            result = await bridge.run_review(review.next_review(step.ref))
        self.assertEqual(result.outcome["judgment"], "APPROVED")
        self.assertEqual(len(calls), 3)

    async def test_pending_receipt_never_relaunches(self) -> None:
        step = review.start_review(
            review.ReviewRequest(
                str(self.plan), seats="oh-one", session_id="oh-unsettled"
            )
        )
        review.claim_review_action(
            review.ClaimRequest(step.ref, step.work_items[0].action_id)
        )
        with (
            patch.object(
                LLM, "agenerate", side_effect=AssertionError("must not relaunch")
            ),
            self.assertRaises(RuntimeConflict),
        ):
            await self.bridge.run_review(review.next_review(step.ref))
        self.assertEqual(
            review.next_review(step.ref).in_flight, (step.work_items[0].action_id,)
        )

    async def test_cancelled_writer_releases_fence_only_after_revocation(self) -> None:
        entered = asyncio.Event()

        async def complete(llm: LLM, **kwargs: object):
            entered.set()
            await asyncio.Event().wait()

        step = build.start_build(
            build.BuildRequest(
                "--seats oh-one --executor crew:executor Write hello.txt", "oh-cancel"
            )
        )
        with patch.object(LLM, "agenerate", complete):
            task = asyncio.create_task(self.bridge.run_build(step))
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertIsNone(
            build._transaction(
                step.ref, lambda data, journal: journal.outstanding_writer
            )
        )
        self.assertTrue(
            all(job.capability._revoked for job in self.runtime.jobs.values())
        )
        result = build.next_build(step.ref)
        self.assertNotEqual(result.type, review.StepType.WORK_BATCH)

    async def test_refused_writer_initialization_settles_claim(self) -> None:
        step = build.start_build(
            build.BuildRequest(
                "--seats oh-one --executor crew:executor Write hello.txt", "oh-refused"
            )
        )
        with patch.object(
            self.runtime,
            "launch",
            side_effect=LaunchRefused("SDK initialization refused"),
        ):
            result = await self.bridge.run_build(step)
        self.assertIsNone(
            build._transaction(
                step.ref, lambda data, journal: journal.outstanding_writer
            )
        )
        self.assertNotEqual(result.type, review.StepType.WORK_BATCH)

    async def test_one_provider_failure_does_not_cancel_other_seat(self) -> None:
        async def complete(llm: LLM, **kwargs: object):
            if llm.model == MODELS[1]:
                raise RuntimeError("fixture provider failure")
            text = str(kwargs.get("messages"))
            report = (
                'Synthesis\nCREW_JUDGMENT: {"verdict":"APPROVED","minor_only":false}'
                if "CREW_JUDGMENT" in text
                else "## VERDICT\nAPPROVED\n\n## FINDINGS\nnone\n"
            )
            return fixture_response("finish", {"message": report})

        step = review.start_review(
            review.ReviewRequest(
                str(self.plan), seats="oh-one,oh-two", session_id="oh-failure"
            )
        )
        with patch.object(LLM, "agenerate", complete):
            result = await self.bridge.run_review(step)
        self.assertEqual(result.type, review.StepType.TERMINAL)
        self.assertEqual(result.outcome["status"], "quorum_not_met")
        self.assertEqual(result.outcome["usable"], 1)

    async def test_explicit_host_does_not_convert_cli_seats(self) -> None:
        from multiagent import seats

        spec = seats.seat_spec("sol")
        execution = channels.resolve_seat(spec, declared_native="openhands")
        self.assertEqual(execution.channel, "codex")
        self.assertFalse(execution.native)


if __name__ == "__main__":
    unittest.main()
