"""Optional native OpenHands action execution; workflow policy remains in Crew."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from importlib.metadata import version
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from openhands.sdk import LLM, Agent
from openhands.sdk.context import AgentContext
from openhands.sdk.conversation.impl.local_conversation import LocalConversation
from openhands.sdk.conversation.state import ConversationExecutionStatus
from openhands.sdk.event import ActionEvent
from openhands.sdk.tool import (
    Action,
    Observation,
    Tool,
    ToolDefinition,
    ToolExecutor,
    register_tool,
)
from openhands.sdk.tool.builtins.finish import FinishAction
from workspace import AccessDenied, Workspace


class RuntimeConflict(ValueError):
    pass


class LaunchRefused(RuntimeError):
    """Initialization failed before arun or any issued tool could execute."""


@dataclass(frozen=True, slots=True)
class NativeAction:
    owner: str
    action_id: str
    model: str
    prompt: str
    workspace: str
    role: Literal[
        "reviewer", "panelist", "advisor", "executor", "formatter", "synthesis"
    ]
    staging_path: str | None = None

    def __post_init__(self) -> None:
        if not self.owner or not self.action_id or not self.model or not self.prompt:
            raise ValueError(
                "An action needs its owner, action ID, exact model and prompt"
            )
        if self.role not in {
            "reviewer",
            "panelist",
            "advisor",
            "executor",
            "formatter",
            "synthesis",
        }:
            raise ValueError("Unsupported native role")
        if (self.role == "advisor") != (self.staging_path is not None):
            raise ValueError("Only an advisor must have an issued plan staging path")
        if not Path(self.workspace).is_absolute():
            raise ValueError("The workspace must be absolute")


@dataclass(frozen=True, slots=True)
class NativeResult:
    owner: str
    action_id: str
    handle: str
    model: str
    status: str
    report: str
    quiescent: bool
    diagnostic: str | None = None


class CrewConversation(LocalConversation):
    """Pinned SDK shim: issued actions never auto-load ambient plugins or hooks."""

    def _ensure_plugins_loaded(self) -> None:
        self._resolved_plugins = []
        self._plugins_loaded = True


class ReadAction(Action):
    operation: Literal["read", "list"]
    path: str


class WriteAction(Action):
    path: str
    content: str


class FileObservation(Observation):
    pass


class FileExecutor(ToolExecutor):
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def __call__(
        self,
        action: ReadAction | WriteAction,
        conversation: LocalConversation | None = None,
    ) -> FileObservation:
        try:
            if isinstance(action, WriteAction):
                self.workspace.write(action.path, action.content)
                result = "File written"
            elif action.operation == "list":
                result = "\n".join(self.workspace.list(action.path))
            else:
                result = self.workspace.read(action.path)
            return FileObservation.from_text(result)
        except (AccessDenied, OSError, UnicodeError) as exc:
            return FileObservation.from_text(
                f"File operation refused: {exc}", is_error=True
            )

    def interrupt(self) -> None:
        self.workspace.revoke()


# SDK registry factories receive the action's capability through the state ID.
# Entries exist only while their conversation is being constructed and initialized.
_capabilities: dict[str, Workspace] = {}


class CrewReadTool(ToolDefinition[ReadAction, FileObservation]):
    @classmethod
    def create(cls, conv_state: object, **params: object) -> Sequence[CrewReadTool]:
        return [
            cls(
                name="crew_read",
                description="Read a UTF-8 file or list a workspace directory. Paths cannot escape the issued workspace.",
                action_type=ReadAction,
                observation_type=FileObservation,
                executor=FileExecutor(_capabilities[str(conv_state.id)]),
            )
        ]


class CrewWriteTool(ToolDefinition[WriteAction, FileObservation]):
    @classmethod
    def create(cls, conv_state: object, **params: object) -> Sequence[CrewWriteTool]:
        return [
            cls(
                name="crew_write",
                description="Write a file, creating parent directories, permitted by the issued action. Metadata and paths outside the workspace are protected.",
                action_type=WriteAction,
                observation_type=FileObservation,
                executor=FileExecutor(_capabilities[str(conv_state.id)]),
            )
        ]


register_tool("CrewReadTool", CrewReadTool)
register_tool("CrewWriteTool", CrewWriteTool)


@dataclass(slots=True)
class _Job:
    action: NativeAction
    handle: str
    directory: Path
    binding: bytes
    capability: Workspace
    conversation: CrewConversation
    task: asyncio.Task[NativeResult] | None = None
    cancelled: bool = False
    started: asyncio.Event = field(default_factory=asyncio.Event)
    running: bool = False


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _write_once(path: Path, data: bytes) -> None:
    if path.is_symlink():
        raise RuntimeConflict("Receipt must be a regular owned file")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if path.read_bytes() != data:
            raise RuntimeConflict("Action differs from its retained receipt") from None
    else:
        with os.fdopen(fd, "wb") as output:
            output.write(data)


class OpenHandsRuntime:
    def __init__(
        self,
        root: Path,
        models: Mapping[str, LLM],
        *,
        max_concurrency: int,
        max_iterations: int = 50,
    ) -> None:
        if version("openhands-sdk") != "1.51.0":
            raise ValueError("This adapter requires the audited openhands-sdk==1.51.0")
        if type(max_iterations) is not int or max_iterations < 1:
            raise ValueError("A positive iteration limit is required")
        if type(max_concurrency) is not int or max_concurrency < 1:
            raise ValueError("An explicit positive concurrency limit is required")
        if root.is_symlink():
            raise RuntimeConflict("Runtime receipts cannot use a symlink root")
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.models = dict(models)
        for name, llm in self.models.items():
            if name != llm.model or llm.fallback_strategy is not None:
                raise ValueError("Model routes must use exact IDs without fallback")
        self.max_iterations = max_iterations
        self.capacity = asyncio.Semaphore(max_concurrency)
        self.jobs: dict[str, _Job] = {}

    def launch(self, action: NativeAction) -> str:
        llm = self.models.get(action.model)
        if llm is None:
            raise ValueError(f"No configured SDK model route for {action.model}")
        identity = {
            **asdict(action),
            "prompt": hashlib.sha256(action.prompt.encode()).hexdigest(),
            "base_url": llm.base_url,
            "model_config_sha256": hashlib.sha256(
                _canonical(llm.model_dump(mode="json"))
            ).hexdigest(),
        }
        binding = _canonical(identity)
        key = hashlib.sha256(_canonical([action.owner, action.action_id])).hexdigest()
        directory = self.root / key
        for job in self.jobs.values():
            if job.directory == directory:
                if job.binding != binding:
                    raise RuntimeConflict("Action identity changed after launch")
                return job.handle
        if directory.exists():
            raise RuntimeConflict(
                "Retained action requires reconciliation, not another launch"
            )
        directory.mkdir(mode=0o700)
        handle = str(uuid4())
        _write_once(
            directory / "binding.json",
            _canonical({"identity": identity, "handle": handle}),
        )
        capability = Workspace(
            Path(action.workspace),
            writable=action.role == "executor",
            staging=Path(action.staging_path) if action.staging_path else None,
        )
        _capabilities[handle] = capability
        conversation = None
        try:
            tools = [Tool(name="CrewReadTool")]
            if action.role in {"advisor", "executor"}:
                tools.append(Tool(name="CrewWriteTool"))
            agent = Agent(
                llm=llm.model_copy(deep=True),
                tools=tools,
                include_default_tools=["FinishTool"],
                agent_context=AgentContext(
                    load_user_skills=False,
                    load_public_skills=False,
                    load_project_skills=False,
                    load_memory=False,
                ),
            )
            workspace = directory / "sdk-workspace"
            workspace.mkdir(mode=0o700)
            conversation = CrewConversation(
                agent=agent,
                workspace=workspace,
                persistence_dir=directory / "sdk",
                conversation_id=UUID(handle),
                visualizer=None,
                max_iteration_per_run=self.max_iterations,
                delete_on_close=False,
                profile_store_dir=directory / "profiles",
            )
            conversation.send_message(action.prompt)
            expected_tools = {"finish", "crew_read"} | (
                {"crew_write"} if action.role in {"advisor", "executor"} else set()
            )
            if set(conversation.agent.tools_map) != expected_tools:
                raise RuntimeConflict(
                    "SDK attached tools outside the issued capability set"
                )
        except Exception as exc:
            capability.close()
            if conversation is not None:
                conversation.close()
            raise LaunchRefused(
                f"SDK initialization refused ({type(exc).__name__})"
            ) from exc
        finally:
            _capabilities.pop(handle, None)
        job = _Job(action, handle, directory, binding, capability, conversation)
        self.jobs[handle] = job
        job.task = asyncio.create_task(self._run(job))
        return handle

    async def _run(self, job: _Job) -> NativeResult:
        status, report, diagnostic = "failed", "", None
        job.started.set()
        try:
            async with self.capacity:
                if not job.cancelled:
                    job.running = True
                    await job.conversation.arun()
            if job.cancelled:
                status, diagnostic = "cancelled", "Owned action interrupted"
            elif (
                job.conversation.state.execution_status
                == ConversationExecutionStatus.FINISHED
            ):
                finishes = [
                    event.action.message
                    for event in job.conversation.state.events
                    if isinstance(event, ActionEvent)
                    and isinstance(event.action, FinishAction)
                ]
                if len(finishes) == 1 and finishes[0].strip():
                    status, report = "ok", finishes[0]
                else:
                    diagnostic = "Finished action has no unique final report"
            else:
                diagnostic = (
                    f"SDK stopped at {job.conversation.state.execution_status.value}"
                )
        except asyncio.CancelledError:
            status, diagnostic = "cancelled", "Owned action interrupted"
        except Exception as exc:  # noqa: BLE001 - provider SDK errors become typed action failures.
            diagnostic = f"SDK execution failed ({type(exc).__name__})"
        finally:
            # Only the capability tools may write; closing waits for their active operation.
            await asyncio.to_thread(job.capability.close)
            job.conversation.close()
        result = NativeResult(
            job.action.owner,
            job.action.action_id,
            job.handle,
            job.action.model,
            status,
            report,
            True,
            diagnostic,
        )
        _write_once(job.directory / "result.json", _canonical(asdict(result)))
        return result

    def retained_result(self, action: NativeAction) -> NativeResult:
        key = hashlib.sha256(_canonical([action.owner, action.action_id])).hexdigest()
        directory = self.root / key
        if directory.is_symlink() or any(
            (directory / name).is_symlink() for name in ("binding.json", "result.json")
        ):
            raise RuntimeConflict("Retained action contains a symlink")
        llm = self.models.get(action.model)
        if llm is None:
            raise RuntimeConflict("No matching model route for retained action")
        expected = {
            **asdict(action),
            "prompt": hashlib.sha256(action.prompt.encode()).hexdigest(),
            "base_url": llm.base_url,
            "model_config_sha256": hashlib.sha256(
                _canonical(llm.model_dump(mode="json"))
            ).hexdigest(),
        }
        try:
            binding = json.loads((directory / "binding.json").read_bytes())
            if binding["identity"] != expected:
                raise RuntimeConflict("Retained action identity differs")
            result = NativeResult(
                **json.loads((directory / "result.json").read_bytes())
            )
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise RuntimeConflict(
                "Action has no matching settled receipt; do not relaunch"
            ) from exc
        if (
            (result.owner, result.action_id, result.model, result.handle)
            != (action.owner, action.action_id, action.model, binding["handle"])
            or result.quiescent is not True
            or result.status not in {"ok", "failed", "cancelled"}
        ):
            raise RuntimeConflict("Retained result does not match the issued action")
        return result

    async def completions(
        self, handles: tuple[str, ...]
    ) -> AsyncIterator[NativeResult]:
        if len(set(handles)) != len(handles) or any(
            handle not in self.jobs for handle in handles
        ):
            raise RuntimeConflict("Completion handles must name distinct owned actions")
        tasks = [asyncio.shield(self.jobs[handle].task) for handle in handles]
        for task in asyncio.as_completed(tasks):
            yield await task

    async def cancel(self, handle: str) -> NativeResult:
        job = self.jobs.get(handle)
        if job is None:
            raise RuntimeConflict("Cannot cancel an unowned action")
        if job.task.done():
            return await job.task
        job.cancelled = True
        # Revoke before interrupting: SDK interruption alone cannot stop a tool thread.
        await asyncio.to_thread(job.capability.revoke)
        await job.started.wait()
        if job.running:
            job.conversation.interrupt()
        else:
            job.task.cancel()
        return await job.task
