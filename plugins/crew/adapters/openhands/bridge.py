"""Transport engine-issued actions through the optional SDK runtime."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path

from multiagent import build_workflow as build
from multiagent import channels
from multiagent import measure_twice as measure
from multiagent import review_workflow as review
from multiagent import workflow_transport as transport
from multiagent.native_binding import NativeLaunch, bind_native_launch
from runtime import LaunchRefused, NativeAction, NativeResult, OpenHandsRuntime
from state_discovery import crew_base


def issued_prompt(prompt_path: str, role: str) -> str:
    instructions = ""
    if role in {"reviewer", "panelist", "advisor", "executor"}:
        source = Path(__file__).resolve().parents[2] / "agents" / f"{role}.md"
        instructions = source.read_text()
        if instructions.startswith("---\n"):
            sections = instructions.split("---\n", 2)
            if len(sections) != 3:
                raise ValueError("Shared role has invalid frontmatter")
            instructions = sections[2]
    return (
        f"Crew adapter role: {role}\n{instructions}\n"
        "Runtime capabilities: crew_read reads/lists the issued workspace; crew_write is "
        "available only to writers and is confined to the issued paths. finish returns the "
        "exact report directly to Crew. No shell, network, delegation, TodoWrite or signal "
        "tool is available. Keep task tracking in your reasoning. If a required command or "
        "verification cannot be performed with the available tools, report it as blocked; "
        "never claim that an unrun check passed. Do not start another Crew workflow.\n"
        "Engine-issued action instructions take precedence over general role guidance:\n"
        + Path(prompt_path).read_text()
    )


class CrewBridge:
    def __init__(self, runtime: OpenHandsRuntime, *, support_model: str) -> None:
        if channels.current_host() != "openhands":
            raise ValueError(
                "The SDK bridge must run in an explicit CREW_HOST=openhands process"
            )
        if support_model not in runtime.models:
            raise ValueError(
                "The formatter/synthesis model must have an explicit SDK route"
            )
        self.runtime = runtime
        self.support_model = support_model

    def _action(
        self, ref: review.ReviewRef, item: review.WorkItem, *, discuss: bool
    ) -> NativeAction:
        if item.driver == "native" and item.channel != "openhands":
            raise ValueError(
                "An OpenHands runtime cannot execute another host's native action"
            )
        model = item.model if item.driver == "native" else self.support_model
        if model not in self.runtime.models:
            raise ValueError(f"No configured SDK model route for {model}")
        role = item.role.split(":")[-1] if item.role else item.kind
        prompt = issued_prompt(item.prompt_path, role)
        if item.kind == "synthesis" and not discuss:
            prompt += '\nReturn the complete synthesis, followed by exactly one line: CREW_JUDGMENT: {"verdict": "APPROVED|REVISE|REJECT", "minor_only": false}. Choose the verdict allowed by the issued instructions. This JSON is transport metadata; do not replace the report with it.\n'
        return NativeAction(
            json.dumps(dataclasses.asdict(ref), sort_keys=True),
            item.action_id,
            model,
            prompt,
            str(crew_base().resolve()),
            role,
        )

    def _capture(
        self,
        ref: review.ReviewRef,
        item: review.WorkItem,
        result: NativeResult,
        *,
        discuss: bool,
    ) -> None:
        if not result.quiescent:
            raise ValueError("Unsettled SDK tools cannot supply a completion")
        judgment = None
        status, diagnostic = result.status, result.diagnostic
        if item.kind == "synthesis" and not discuss and status == "ok":
            try:
                markers = [
                    line.removeprefix("CREW_JUDGMENT: ")
                    for line in result.report.splitlines()
                    if line.startswith("CREW_JUDGMENT: ")
                ]
                if len(markers) != 1:
                    raise ValueError("Expected one explicit synthesis judgment")
                payload = json.loads(markers[0])
                if (
                    set(payload) != {"verdict", "minor_only"}
                    or type(payload["minor_only"]) is not bool
                ):
                    raise ValueError("Malformed synthesis judgment")
                judgment = transport.SynthesisJudgment(**payload)
            except (TypeError, ValueError) as exc:
                status, diagnostic = "failed", str(exc)
        kwargs = (
            {"handle": result.handle, "completion_observed": True}
            if item.driver == "native"
            else {}
        )
        transport.capture_review_return(
            ref,
            item.action_id,
            result.report.encode(),
            status=status,
            judgment=judgment,
            diagnostic=diagnostic,
            **kwargs,
        )

    async def review_batch(
        self, step: review.ReviewStep, *, discuss: bool = False, advance: bool = True
    ) -> review.ReviewStep:
        if step.type != review.StepType.WORK_BATCH or step.in_flight:
            return step
        # Validate every route before any claim so configuration failures consume no action.
        actions = {
            item.action_id: self._action(step.ref, item, discuss=discuss)
            for item in step.work_items
            if item.driver != "external"
        }
        handles: dict[str, review.WorkItem] = {}
        external: list[asyncio.Task] = []
        try:
            for item in step.work_items:
                if item.driver == "external":
                    external.append(
                        asyncio.create_task(
                            asyncio.to_thread(
                                review.execute_external_review, step.ref, item.action_id
                            )
                        )
                    )
                    continue
                claim = review.claim_review_action(
                    review.ClaimRequest(step.ref, item.action_id)
                )
                if claim.authorization not in {"spawn", "perform"}:
                    continue
                if claim.work_item != item:
                    raise ValueError("Action differs from its authoritative claim")
                try:
                    handle = self.runtime.launch(actions[item.action_id])
                except LaunchRefused as exc:
                    flags = {"launch_refused": True} if item.driver == "native" else {}
                    transport.capture_review_return(
                        step.ref,
                        item.action_id,
                        b"",
                        status="failed",
                        diagnostic=str(exc),
                        **flags,
                    )
                    continue
                handles[handle] = item
                if item.driver == "native":
                    bind_native_launch(
                        NativeLaunch(step.ref, item.action_id, handle),
                        channel="openhands",
                    )
            async for result in self.runtime.completions(tuple(handles)):
                item = handles[result.handle]
                self._capture(step.ref, item, result, discuss=discuss)
                handles.pop(result.handle)
            if external:
                await asyncio.gather(*external)
        except BaseException:
            # Never cancel the asyncio wrappers around external subprocess owners.
            for handle, item in handles.items():
                result = await self.runtime.cancel(handle)
                try:
                    self._capture(step.ref, item, result, discuss=discuss)
                except review.WorkflowError:
                    pass  # The original error wins; engine ownership and receipts remain intact.
            if external:
                await asyncio.gather(*external, return_exceptions=True)
            raise
        return review.next_review(step.ref) if advance else step

    async def run_review(
        self, step: review.ReviewStep, *, discuss: bool = False
    ) -> review.ReviewStep:
        while True:
            if step.in_flight:
                self.reconcile_review(step.ref, step.in_flight, discuss=discuss)
                step = review.next_review(step.ref)
            elif step.type == review.StepType.WORK_BATCH:
                step = await self.review_batch(step, discuss=discuss)
            elif discuss and (step.outcome or {}).get("status") == "round_complete":
                step = review.next_review(step.ref)
            else:
                return step

    async def _loop_review(self, step: measure.MeasureStep | build.BuildStep) -> None:
        refs = {item.review_ref for item in step.work_items}
        if len(refs) != 1 or None in refs:
            raise ValueError("Loop review batch has mismatched ownership")
        nested = review.ReviewStep(
            review.StepType.WORK_BATCH,
            ref=next(iter(refs)),
            work_items=tuple(item.review_item for item in step.work_items),
        )
        await self.review_batch(nested, advance=False)

    def _loop_action(self, step: measure.MeasureStep | build.BuildStep) -> NativeAction:
        if len(step.work_items) != 1:
            raise ValueError("A writer batch must contain exactly one owned action")
        item = step.work_items[0]
        if item.driver != "native" or item.channel != "openhands":
            raise ValueError(
                "Loop action does not belong to the OpenHands native route"
            )
        role = "advisor" if isinstance(step, measure.MeasureStep) else "executor"
        if item.model not in self.runtime.models:
            raise ValueError(f"No configured SDK model route for {item.model}")
        prompt = issued_prompt(item.prompt_path, role)
        return NativeAction(
            json.dumps(dataclasses.asdict(step.ref), sort_keys=True),
            item.action_id,
            item.model,
            prompt,
            str(crew_base().resolve()),
            role,
            item.staging_path if role == "advisor" else None,
        )

    async def _writer(self, step: measure.MeasureStep | build.BuildStep) -> None:
        action = self._loop_action(step)
        item = step.work_items[0]
        planning = isinstance(step, measure.MeasureStep)
        claim = (
            measure.claim_measure_action if planning else build.claim_build_action
        )(step.ref, item.action_id)
        if claim["authorization"] != "spawn":
            return
        expected = (
            measure.work_to_dict(item, transport=True)
            if planning
            else dataclasses.asdict(item)
        )
        if claim["work_item"] != expected:
            raise ValueError("Writer differs from its authoritative claim")
        capture = (
            transport.capture_measure_return if planning else build.capture_build_return
        )
        try:
            handle = self.runtime.launch(action)
        except LaunchRefused as exc:
            capture(
                step.ref,
                item.action_id,
                b"",
                status="failed",
                diagnostic=str(exc),
                launch_refused=True,
            )
            return
        try:
            bind_native_launch(
                NativeLaunch(step.ref, item.action_id, handle), channel="openhands"
            )
            async for result in self.runtime.completions((handle,)):
                capture = (
                    transport.capture_measure_return
                    if planning
                    else build.capture_build_return
                )
                capture(
                    step.ref,
                    item.action_id,
                    result.report.encode(),
                    status=result.status,
                    diagnostic=result.diagnostic,
                    handle=handle,
                    completion_observed=True,
                )
        except BaseException:
            result = await self.runtime.cancel(handle)
            try:
                capture(
                    step.ref,
                    item.action_id,
                    result.report.encode(),
                    status=result.status,
                    diagnostic=result.diagnostic,
                    handle=handle,
                    completion_observed=True,
                )
            except review.WorkflowError:
                pass  # Never release an uncertain engine fence by inventing settlement.
            raise

    def reconcile_review(
        self,
        ref: review.ReviewRef,
        action_ids: tuple[str, ...],
        *,
        discuss: bool = False,
    ) -> None:
        """Replay settled SDK receipts into pending Crew claims; never relaunch."""
        run = review._guard_review_path(
            session_segment=ref.session_segment, run_id=ref.run_id, create=False
        )
        for action_id in action_ids:
            with review._owned_workflow_lock(run, ref):
                wf, _ = review._load_current_locked(ref, run)
                action = review._load_action(wf, action_id)
                if (
                    review._action_attempt(action) != ref.attempt_id
                    or action["status"] != "claimed"
                ):
                    raise ValueError(
                        "Reconciliation requires the current pending action"
                    )
                item = review._work_item(
                    action,
                    ref,
                    loop_owned=wf["workflow_identity"]["kind"] == "loop_review",
                )
            if item.driver == "external":
                raise ValueError("External actions must use Crew's provider recovery")
            result = self.runtime.retained_result(
                self._action(ref, item, discuss=discuss)
            )
            if item.driver == "native":
                bind_native_launch(
                    NativeLaunch(ref, item.action_id, result.handle),
                    channel="openhands",
                )
            self._capture(ref, item, result, discuss=discuss)

    def reconcile_loop(self, step: measure.MeasureStep | build.BuildStep) -> None:
        planning = isinstance(step, measure.MeasureStep)
        transaction = measure._transaction if planning else build._transaction

        def pending(_data, journal):
            if (
                journal.action is not None
                and (
                    journal.action.item.action_id
                    if planning
                    else journal.action.action_id
                )
                in step.in_flight
            ):
                item = (
                    journal.action.item if planning else build._item(step.ref, journal)
                )
                return item, None
            return None, journal.review_ref

        item, nested_ref = transaction(step.ref, pending)
        if item is None:
            if nested_ref is None:
                raise ValueError("No owned pending action to reconcile")
            self.reconcile_review(nested_ref, step.in_flight)
            return
        issued = dataclasses.replace(step, work_items=(item,))
        result = self.runtime.retained_result(self._loop_action(issued))
        bind_native_launch(
            NativeLaunch(step.ref, item.action_id, result.handle), channel="openhands"
        )
        capture = (
            transport.capture_measure_return if planning else build.capture_build_return
        )
        capture(
            step.ref,
            item.action_id,
            result.report.encode(),
            status=result.status,
            diagnostic=result.diagnostic,
            handle=result.handle,
            completion_observed=True,
        )

    async def run_measure(self, step: measure.MeasureStep) -> measure.MeasureStep:
        while step.type == review.StepType.WORK_BATCH or step.in_flight:
            if step.in_flight:
                self.reconcile_loop(step)
            elif step.work_items[0].review_ref is not None:
                await self._loop_review(step)
            else:
                await self._writer(step)
            step = measure.next_measure_twice(step.ref)
        return step

    async def run_build(self, step: build.BuildStep) -> build.BuildStep:
        while step.type == review.StepType.WORK_BATCH or step.in_flight:
            if step.in_flight:
                self.reconcile_loop(step)
            else:
                item = step.work_items[0]
                if item.review_ref is not None:
                    await self._loop_review(step)
                elif item.driver == "external":
                    await asyncio.to_thread(
                        build.execute_build_action, step.ref, item.action_id
                    )
                else:
                    await self._writer(step)
            step = build.next_build(step.ref)
        return step
