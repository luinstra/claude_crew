"""Offline probes against the real SDK; provider responses alone are fixtures."""

from __future__ import annotations

import asyncio
import atexit
import json
import os
import socket
import tempfile
import threading
import unittest
from collections.abc import Sequence
from pathlib import Path
from unittest.mock import patch

_STATE = tempfile.TemporaryDirectory(prefix="crew-openhands-contract-")
atexit.register(_STATE.cleanup)
os.environ["OH_PERSISTENCE_DIR"] = _STATE.name
os.environ["OPENHANDS_SUPPRESS_BANNER"] = "1"
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"

from litellm import ModelResponse
from openhands.sdk import LLM, Agent, Conversation
from openhands.sdk.conversation.state import ConversationExecutionStatus
from openhands.sdk.event import ActionEvent
from openhands.sdk.llm import Message, MessageToolCall
from openhands.sdk.llm.llm_response import LLMResponse
from openhands.sdk.llm.utils.metrics import MetricsSnapshot
from openhands.sdk.tool import Action, Observation, ToolDefinition, ToolExecutor
from openhands.sdk.tool.builtins.finish import FinishAction

MODELS = ("openai/gpt-4o", "anthropic/claude-sonnet-4-5-20250929")


def fixture_response(name: str, arguments: dict[str, str]) -> LLMResponse:
    return LLMResponse(
        message=Message(
            role="assistant",
            content=[],
            tool_calls=[
                MessageToolCall(
                    id="probe-call",
                    name=name,
                    arguments=json.dumps(arguments),
                    origin="completion",
                )
            ],
        ),
        metrics=MetricsSnapshot(),
        raw_response=ModelResponse(id="offline-response"),
    )


def final_report(conversation: Conversation) -> str:
    if conversation.state.execution_status != ConversationExecutionStatus.FINISHED:
        raise ValueError("Only a finished conversation can supply a successful report")
    finishes = [
        event.action.message
        for event in conversation.state.events
        if isinstance(event, ActionEvent) and isinstance(event.action, FinishAction)
    ]
    if len(finishes) != 1:
        raise ValueError("Expected exactly one finish in this fresh action")
    return finishes[0]


class ProbeWrite(Action):
    pass


class ProbeObservation(Observation):
    pass


class ProbeWriter(ToolExecutor):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.entered = threading.Event()
        self.release = threading.Event()
        self.exited = threading.Event()
        self.interrupted = threading.Event()

    def __call__(
        self, action: ProbeWrite, conversation: Conversation | None = None
    ) -> ProbeObservation:
        self.entered.set()
        try:
            if not self.release.wait(5):
                raise TimeoutError("test must release its owned writer")
            self.path.write_text("write completed after interrupt")
            return ProbeObservation.from_text("done")
        finally:
            self.exited.set()

    def interrupt(self) -> None:
        self.interrupted.set()


class ProbeWriteTool(ToolDefinition[ProbeWrite, ProbeObservation]):
    @classmethod
    def create(
        cls, conv_state: object = None, **params: object
    ) -> Sequence[ProbeWriteTool]:
        raise NotImplementedError("The test injects its own tracked executor")


class SdkContract(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.root = tempfile.TemporaryDirectory(prefix="crew-openhands-workspace-")
        self.addCleanup(self.root.cleanup)
        self.conversations: list[Conversation] = []
        self.addCleanup(
            lambda: [conversation.close() for conversation in self.conversations]
        )
        self.network = patch.object(
            socket.socket, "connect", side_effect=AssertionError("offline test")
        )
        self.network.start()
        self.addCleanup(self.network.stop)
        # Ambient hooks are outside this fixture's contract, not a production isolation mechanism.
        self.plugins = patch(
            "openhands.sdk.conversation.impl.local_conversation.load_available_plugins",
            return_value={},
        )
        self.plugins.start()
        self.addCleanup(self.plugins.stop)

    def conversation(self, model: str = MODELS[0]) -> Conversation:
        llm = LLM(
            model=model,
            api_key="offline-fixture",
            max_input_tokens=32768,
            max_output_tokens=1024,
        )
        conversation = Conversation(
            agent=Agent(llm=llm, tools=[], include_default_tools=["FinishTool"]),
            workspace=self.root.name,
            persistence_dir=Path(self.root.name) / "events",
            visualizer=None,
            max_iteration_per_run=3,
        )
        self.conversations.append(conversation)
        conversation.send_message(
            "Perform one issued role action and finish with the exact report"
        )
        return conversation

    async def test_concurrent_native_conversations_keep_exact_reports(self) -> None:
        entered: set[str] = set()
        release = asyncio.Event()

        async def complete(llm: LLM, **kwargs: object) -> LLMResponse:
            entered.add(llm.model)
            if len(entered) == 2:
                release.set()
            await asyncio.wait_for(release.wait(), 5)
            return fixture_response(
                "finish", {"message": f"raw {llm.model}\n\n  whitespace  \n"}
            )

        conversations = [self.conversation(model) for model in MODELS]
        with patch.object(LLM, "agenerate", complete):
            await asyncio.wait_for(
                asyncio.gather(*(c.arun() for c in conversations)), 10
            )
        self.assertEqual(entered, set(MODELS))
        self.assertNotEqual(conversations[0].id, conversations[1].id)
        for conversation in conversations:
            self.assertEqual(
                final_report(conversation),
                f"raw {conversation.agent.llm.model}\n\n  whitespace  \n",
            )
            self.assertEqual(set(conversation.agent.tools_map), {"finish"})

    async def test_missing_finish_is_not_a_report(self) -> None:
        conversation = self.conversation()
        with self.assertRaises(ValueError):
            final_report(conversation)

    async def test_unissued_write_tool_is_rejected(self) -> None:
        conversation = self.conversation()
        calls = 0

        async def attempt_write(llm: LLM, **kwargs: object) -> LLMResponse:
            nonlocal calls
            calls += 1
            if calls == 1:
                return fixture_response("probe_write", {})
            return fixture_response("finish", {"message": "write denied"})

        with patch.object(LLM, "agenerate", attempt_write):
            await asyncio.wait_for(conversation.arun(), 5)
        self.assertEqual(calls, 2)
        self.assertEqual(final_report(conversation), "write denied")
        self.assertEqual(set(conversation.agent.tools_map), {"finish"})
        self.assertFalse((Path(self.root.name) / "late-write.txt").exists())

    async def test_interrupt_during_model_wait_is_not_success(self) -> None:
        entered = asyncio.Event()

        async def wait_forever(llm: LLM, **kwargs: object) -> LLMResponse:
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        conversation = self.conversation()
        with patch.object(LLM, "agenerate", wait_forever):
            task = asyncio.create_task(conversation.arun())
            await asyncio.wait_for(entered.wait(), 5)
            conversation.interrupt()
            await asyncio.wait_for(task, 5)
        self.assertEqual(
            conversation.state.execution_status, ConversationExecutionStatus.PAUSED
        )
        with self.assertRaises(ValueError):
            final_report(conversation)

    async def test_interrupt_does_not_prove_tool_quiescence(self) -> None:
        conversation = self.conversation()
        writer = ProbeWriter(Path(self.root.name) / "late-write.txt")
        conversation.agent.add_runtime_tools(
            [
                ProbeWriteTool(
                    name="probe_write",
                    description="Disposable test writer",
                    action_type=ProbeWrite,
                    observation_type=ProbeObservation,
                    executor=writer,
                )
            ]
        )

        async def write(llm: LLM, **kwargs: object) -> LLMResponse:
            return fixture_response("probe_write", {})

        try:
            with patch.object(LLM, "agenerate", write):
                task = asyncio.create_task(conversation.arun())
                self.assertTrue(await asyncio.to_thread(writer.entered.wait, 5))
                conversation.interrupt()
                await asyncio.wait_for(task, 5)
            self.assertTrue(writer.interrupted.is_set())
            self.assertFalse(writer.exited.is_set())
            self.assertFalse(writer.path.exists())
            writer.release.set()
            self.assertTrue(await asyncio.to_thread(writer.exited.wait, 5))
            self.assertEqual(writer.path.read_text(), "write completed after interrupt")
        finally:
            writer.release.set()
            await asyncio.to_thread(writer.exited.wait, 5)


if __name__ == "__main__":
    unittest.main()
