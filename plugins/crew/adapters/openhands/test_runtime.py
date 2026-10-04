"""SDK execution tests with real conversations and offline model responses."""

from __future__ import annotations

import asyncio
import dataclasses
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from test_sdk_contract import LLM, MODELS, fixture_response

# isort: split
from runtime import NativeAction, OpenHandsRuntime, RuntimeConflict
from workspace import AccessDenied, Workspace


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "source"
        self.workspace.mkdir()
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
            self.root / "receipts", self.models, max_concurrency=2
        )

    def action(
        self,
        ident: str = "review-1",
        *,
        model: str = MODELS[0],
        role: str = "reviewer",
        staging: str | None = None,
    ) -> NativeAction:
        return NativeAction(
            "owner-1",
            ident,
            model,
            "Perform this exact issued action",
            str(self.workspace),
            role,
            staging,
        )

    async def test_parallel_capture_replay_and_drift(self) -> None:
        entered = set()
        release = asyncio.Event()

        async def complete(llm: LLM, **kwargs: object):
            entered.add(llm.model)
            if len(entered) == 2:
                release.set()
            await asyncio.wait_for(release.wait(), 5)
            return fixture_response(
                "finish", {"message": f"report {llm.model}\n  exact  \n"}
            )

        actions = [self.action(str(i), model=model) for i, model in enumerate(MODELS)]
        with patch.object(LLM, "agenerate", complete):
            handles = tuple(self.runtime.launch(action) for action in actions)
            self.assertEqual(self.runtime.launch(actions[0]), handles[0])
            results = [r async for r in self.runtime.completions(handles)]
        self.assertEqual(len(entered), 2)
        self.assertTrue(all(r.status == "ok" and r.quiescent for r in results))
        for result in results:
            self.assertEqual(result.report, f"report {result.model}\n  exact  \n")
        restarted = OpenHandsRuntime(
            self.root / "receipts", self.models, max_concurrency=2
        )
        self.assertEqual(restarted.retained_result(actions[0]).handle, handles[0])
        with self.assertRaises(RuntimeConflict):
            restarted.launch(actions[0])
        with self.assertRaises(RuntimeConflict):
            restarted.retained_result(dataclasses.replace(actions[0], prompt="changed"))

    async def test_executor_has_only_confined_file_tools(self) -> None:
        calls = 0

        async def complete(llm: LLM, **kwargs: object):
            nonlocal calls
            calls += 1
            if calls == 1:
                return fixture_response(
                    "crew_write", {"path": "hello.txt", "content": "hello"}
                )
            return fixture_response("finish", {"message": "implemented"})

        with patch.object(LLM, "agenerate", complete):
            handle = self.runtime.launch(self.action(role="executor"))
            result = await anext(self.runtime.completions((handle,)))
        self.assertEqual(result.status, "ok")
        self.assertEqual((self.workspace / "hello.txt").read_text(), "hello")
        self.assertEqual(
            set(self.runtime.jobs[handle].conversation.agent.tools_map),
            {"crew_read", "crew_write", "finish"},
        )
        with self.assertRaises(AccessDenied):
            self.runtime.jobs[handle].capability.write("late.txt", "late")

    async def test_cancelled_model_wait_settles_and_revokes(self) -> None:
        entered = asyncio.Event()

        async def complete(llm: LLM, **kwargs: object):
            entered.set()
            await asyncio.Event().wait()

        with patch.object(LLM, "agenerate", complete):
            handle = self.runtime.launch(self.action(role="executor"))
            await asyncio.wait_for(entered.wait(), 5)
            result = await asyncio.wait_for(self.runtime.cancel(handle), 5)
        self.assertEqual(result.status, "cancelled")
        self.assertTrue(result.quiescent)
        with self.assertRaises(AccessDenied):
            self.runtime.jobs[handle].capability.write("late.txt", "late")

    async def test_cancel_queued_action_does_not_wait_for_unrelated_model(self) -> None:
        runtime = OpenHandsRuntime(self.root / "serial", self.models, max_concurrency=1)
        entered = asyncio.Event()
        calls = []

        async def complete(llm: LLM, **kwargs: object):
            calls.append(llm.model)
            entered.set()
            await asyncio.Event().wait()

        with patch.object(LLM, "agenerate", complete):
            first = runtime.launch(self.action("first"))
            await asyncio.wait_for(entered.wait(), 5)
            second = runtime.launch(self.action("queued", model=MODELS[1]))
            try:
                result = await asyncio.wait_for(runtime.cancel(second), 5)
                self.assertEqual(result.status, "cancelled")
                self.assertEqual(calls, [MODELS[0]])
            finally:
                await runtime.cancel(first)

    async def test_cancel_waits_for_active_file_operation(self) -> None:
        entered, release = threading.Event(), threading.Event()
        original = Workspace.write

        def writing(capability, path, content):
            with capability._lock:
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("fixture writer was not released")
                original(capability, path, content)

        calls = 0

        async def complete(llm: LLM, **kwargs: object):
            nonlocal calls
            calls += 1
            if calls == 1:
                return fixture_response(
                    "crew_write", {"path": "active.txt", "content": "settled"}
                )
            await asyncio.Event().wait()

        with (
            patch.object(LLM, "agenerate", complete),
            patch.object(Workspace, "write", writing),
        ):
            handle = self.runtime.launch(self.action(role="executor"))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                cancellation = asyncio.create_task(self.runtime.cancel(handle))
                await asyncio.sleep(0.03)
                self.assertFalse(cancellation.done())
                release.set()
                result = await asyncio.wait_for(cancellation, 5)
            finally:
                release.set()
        self.assertTrue(result.quiescent)
        self.assertEqual(result.status, "cancelled")
        self.assertEqual((self.workspace / "active.txt").read_text(), "settled")

    async def test_empty_finish_is_failed_and_duplicate_handle_refused(self) -> None:
        async def complete(llm: LLM, **kwargs: object):
            return fixture_response("finish", {"message": "  "})

        with patch.object(LLM, "agenerate", complete):
            handle = self.runtime.launch(self.action())
            with self.assertRaises(RuntimeConflict):
                _ = [r async for r in self.runtime.completions((handle, handle))]
            result = await anext(self.runtime.completions((handle,)))
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.report, "")

    async def test_no_ambient_plugin_or_model_fallback(self) -> None:
        async def complete(llm: LLM, **kwargs: object):
            return fixture_response("finish", {"message": "done"})

        with (
            patch(
                "openhands.sdk.conversation.impl.local_conversation.load_available_plugins",
                side_effect=AssertionError("ambient discovery must not run"),
            ),
            patch.object(LLM, "agenerate", complete),
        ):
            handle = self.runtime.launch(self.action())
            result = await anext(self.runtime.completions((handle,)))
        self.assertEqual(result.status, "ok")
        with self.assertRaises(ValueError):
            self.runtime.launch(self.action(model="unconfigured/model"))
        with self.assertRaises(RuntimeConflict):
            await self.runtime.cancel("other-agent")


class WorkspaceTests(unittest.TestCase):
    def test_readonly_traversal_symlink_hardlink_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "repo"
            workspace.mkdir()
            secret = root / "outside.txt"
            secret.write_text("outside")
            (workspace / "linked").symlink_to(secret)
            (workspace / "hard").hardlink_to(secret)
            (workspace / ".git").mkdir()
            capabilities = Workspace(workspace, writable=True)
            try:
                for path in (
                    "../outside.txt",
                    str(secret),
                    "linked",
                    "hard",
                    ".git/config",
                ):
                    with self.assertRaises((AccessDenied, OSError)):
                        capabilities.write(path, "changed")
                self.assertEqual(secret.read_text(), "outside")
                capabilities.write("nested/new.txt", "new")
                self.assertEqual((workspace / "nested/new.txt").read_text(), "new")
                capabilities.write("okay.txt", "okay")
                capabilities.revoke()
                with self.assertRaises(AccessDenied):
                    capabilities.write("okay.txt", "late")
            finally:
                capabilities.close()
            readonly = Workspace(workspace)
            try:
                self.assertEqual(readonly.read("okay.txt"), "okay")
                with self.assertRaises(AccessDenied):
                    readonly.write("okay.txt", "changed")
            finally:
                readonly.close()

    def test_advisor_can_write_only_issued_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".crew").mkdir()
            staging = root / ".crew" / "issued-plan.md"
            capability = Workspace(root, staging=staging)
            try:
                capability.write(str(staging), "plan")
                for path in ("source.py", ".crew/state.json"):
                    with self.assertRaises(AccessDenied):
                        capability.write(path, "unissued")
                self.assertEqual(staging.read_text(), "plan")
            finally:
                capability.close()


if __name__ == "__main__":
    unittest.main()
