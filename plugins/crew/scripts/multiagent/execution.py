"""Write execution, frozen executor resolution and workspace observations."""

from __future__ import annotations

import contextlib
import datetime
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass

from models import LOAD_MISSING, LOAD_OK, LoopState
from state_discovery import crew_base, find_session_state_file

from multiagent import channels, config, continuations, seats
from multiagent.providers import (
    ContinuationOutcome,
    ProviderContinuation,
    ProviderResult,
    get_provider,
    known_seat_names,
)

_UNBORN = "<unborn>"
_DETACHED = "<detached HEAD>"


class ExecutionError(Exception):
    pass


class ExecutionBusy(ExecutionError):
    pass


@dataclass(frozen=True, slots=True)
class WorkspaceFacts:
    head: str | None
    index_tree: str | None
    branch: str | None

    @property
    def complete(self) -> bool:
        return all(
            value is not None for value in (self.head, self.index_tree, self.branch)
        )


@dataclass(frozen=True, slots=True)
class WriteRequest:
    seat: str
    prompt: str
    model: str | None
    timeout: int
    dispatch_options: dict[str, object]
    workspace: str
    session_id: str = ""
    chain: str | None = None
    loop_instance_id: str = ""
    build_report: bool = False


@dataclass(frozen=True, slots=True)
class ExecutorSelection:
    executor: str
    retries: int
    resume_executor: bool
    source: str
    channel: str | None


def resolve_executor(session_id: str, explicit: str | None = None) -> ExecutorSelection:
    path = find_session_state_file(crew_base() / ".crew", "build-state", session_id)
    state, status = (
        LoopState.load_with_status(path)
        if path is not None
        else (LoopState(), LOAD_MISSING)
    )
    if status not in (LOAD_OK, LOAD_MISSING):
        raise ExecutionError(
            f"build executor cannot resolve over an unreadable build state ({status}): {path}"
        )
    if status == LOAD_OK and state.active is True and state.executor.strip():
        if state.session_id != session_id:
            stamped_session = state.session_id or "<empty>"
            raise ExecutionError(
                f"build state session mismatch in {path}: the loop's stamped session id {stamped_session!r} does not match requested session id {session_id!r}"
            )
        executor, source = state.executor, "state"
        resume = (
            state.resume_executor
            if isinstance(state.resume_executor, bool)
            else config.build_resume_executor()
        )
    else:
        configured = config.build_executor()
        executor = explicit if explicit is not None else configured or "crew:executor"
        source = (
            "flag"
            if explicit is not None
            else "config"
            if configured is not None
            else "builtin"
        )
        resume = config.build_resume_executor()
    channel = channels.task_native_channel(channels.current_host())
    if executor != "crew:executor":
        spec = seats.seat_spec(executor)
        resolved = (
            channels.resolve_seat(spec, declared_native=channel)
            if spec is not None
            else None
        )
        if spec is None or (
            resolved is not None and (resolved.native or not resolved.engine_runnable)
        ):
            raise ExecutionError(
                f"build executor {executor!r} is not an engine-runnable registered seat (a Task seat like opus/sonnet is orchestrator-owned and unrunnable by the engine; a group token like cursor names no single seat); registered seats: {', '.join(known_seat_names())}"
            )
        if resolved is None:
            raise ExecutionError(
                f"executor seat '{executor}' resolves to no eligible execution channel"
            )
        if not resolved.supports_workspace_write:
            raise ExecutionError(
                f"build executor {executor!r} is read-only (does not support workspace-write); the build seam dispatches it in write mode, so it cannot implement the task"
            )
        channel = resolved.channel
    return ExecutorSelection(
        executor, config.build_executor_retries() or 0, resume, source, channel
    )


def observe_workspace(workspace: str) -> WorkspaceFacts:
    return WorkspaceFacts(
        _git_head(workspace), _git_staged(workspace), _git_branch(workspace)
    )


def execute_write(
    request: WriteRequest,
    *,
    provider: object | None = None,
    observers: tuple[Callable, Callable, Callable] | None = None,
    warning_builder: Callable | None = None,
    before_run: Callable[[WorkspaceFacts], None] | None = None,
    on_settle: Callable[[dict, ProviderResult], None] | None = None,
    settlement: Callable[[Callable[[], dict], ProviderResult], dict] | None = None,
) -> dict:
    provider = provider if provider is not None else get_provider(request.seat)
    spec = seats.seat_spec(request.seat)
    kind = spec.provider
    seat, repo_dir = request.seat, request.workspace
    dispatch_opts = request.dispatch_options
    chain, sid = request.chain, request.session_id
    chain_requested = chain is not None
    pre_loop_instance_id = request.loop_instance_id
    workspace = continuations.canonical_workspace(repo_dir)
    model_identity = continuations.resolve_model_identity(request.model, spec.model)
    _git_head, _git_staged, _git_branch = observers or (
        globals()["_git_head"],
        globals()["_git_staged"],
        globals()["_git_branch"],
    )
    warnings = warning_builder or _dispatch_guard_warnings
    lock_cm = (
        continuations.continuation_lock(
            sid,
            chain,
            timeout=continuations.DEFAULT_CHAIN_LOCK_TIMEOUT_SECONDS,
        )
        if chain_requested
        else contextlib.nullcontext()
    )

    lock_acquiring = chain_requested
    try:
        with lock_cm:
            lock_acquiring = False
            # Capture git BEFORE-state (all in repo_dir).
            head_before = _git_head(repo_dir)
            staged_before = _git_staged(repo_dir)
            branch_before = _git_branch(repo_dir)
            before_facts = WorkspaceFacts(head_before, staged_before, branch_before)

            mem_record = None
            expected = None
            binding_dims = ()
            continuation = None
            if chain_requested:
                mem_record = continuations.load_record(sid, chain)
                pre_snapshot_buildable = (
                    head_before is not None
                    and staged_before is not None
                    and branch_before is not None
                    and bool(model_identity)
                )
                if not pre_snapshot_buildable:
                    if before_run is not None and not before_facts.complete:
                        before_run(before_facts)
                    raise ExecutionError(
                        "cannot establish a buildable pre-run workspace snapshot for --chain"
                    )
                expected = continuations.ContinuationBinding(
                    chain=chain,
                    loop_instance_id=pre_loop_instance_id,
                    seat=seat,
                    provider=kind,
                    model=model_identity,
                    workspace=workspace,
                    head=head_before,
                    branch=branch_before,
                    index_tree=staged_before,
                ).normalized()
                if mem_record is not None:
                    binding_dims = continuations.binding_mismatches(
                        mem_record,
                        expected,
                    )
                if (
                    provider.supports_continuation
                    and mem_record is not None
                    and not binding_dims
                    and continuations.valid_conversation_id(
                        mem_record.conversation_id,
                    )
                ):
                    continuation = ProviderContinuation(
                        conversation_id=mem_record.conversation_id,
                    )
                elif provider.supports_continuation:
                    continuation = ProviderContinuation()

            if before_run is not None:
                before_run(before_facts)

            # (e) Availability — unavailable-seat skip (mirrors cmd_run EXACTLY): build a
            # SKIPPED ok=false envelope, the seat NEVER runs (after==before, all guards
            # false), write + print the path, exit 0 under --json. DISTINCT from the
            # exit-2 pre-run rejections in (b).
            # Check availability, then perform the optional pre-run tombstone.
            avail, diag = provider.is_available()
            if not avail:
                result = ProviderResult(
                    name=seat,
                    model=None,
                    ok=False,
                    output="",
                    error=f"skipped: {diag}",
                    elapsed=0.0,
                    transport_status="unavailable",
                )
                head_after, staged_after, branch_after = (
                    head_before,
                    staged_before,
                    branch_before,
                )
            else:
                if chain_requested:
                    try:
                        continuations.invalidate(sid, chain)
                    except Exception as exc:
                        raise ExecutionError(
                            f"could not prepare continuation chain {chain!r}: {exc}"
                        ) from exc
                # (f) Run the seat in workspace-write. Model precedence mirrors cmd_run.
                # This is the ONLY run() call site that passes dispatch_options (the
                # read-only review/debate/probe paths never do).
                timeout = request.timeout
                extra = {"continuation": continuation} if chain_requested else {}
                if request.build_report:
                    extra["build_report"] = True
                result = provider.run(
                    request.prompt,
                    sandbox="workspace-write",
                    model=request.model,
                    timeout=timeout,
                    dispatch_options=dispatch_opts or None,
                    workspace=workspace,
                    **extra,
                )
                # Capture git AFTER-state (all in repo_dir).
                head_after = _git_head(repo_dir)
                staged_after = _git_staged(repo_dir)
                branch_after = _git_branch(repo_dir)

            # Compute the three guards NULL-SAFE (typed booleans, never null)
            # on the internal tri-state values.
            head_moved = (
                head_before is not None
                and head_after is not None
                and head_before != head_after
            )
            staged_changed = (
                staged_before is not None
                and staged_after is not None
                and staged_before != staged_after
            )
            branch_changed = (
                branch_before is not None
                and branch_after is not None
                and branch_before != branch_after
            )

            # Build the guard WARNING lines ONCE (single source), so the --json envelope
            # carries the SAME recovery strings the human path prints. The seam that reads
            # the envelope (build.md) relays these verbatim on a guard violation, rather
            # than re-deriving prose from the booleans. Empty list when no guard fired.
            guard_warnings = warnings(
                head_moved,
                head_before,
                head_after,
                staged_changed,
                branch_changed,
                branch_before,
                branch_after,
            )

            def finish() -> dict:
                chain_status = None
                chain_reset_reason = None
                chain_resumed = False
                if chain_requested:
                    post_snapshot_buildable = (
                        head_after is not None
                        and staged_after is not None
                        and branch_after is not None
                    )
                    post_state_path = find_session_state_file(
                        crew_base() / ".crew",
                        "build-state",
                        sid,
                    )
                    post_state, post_state_status = (
                        LoopState.load_with_status(post_state_path)
                        if post_state_path is not None
                        else (LoopState(), LOAD_MISSING)
                    )
                    state_ok = (
                        post_snapshot_buildable
                        and post_state_status == LOAD_OK
                        and post_state.active is True
                        and post_state.phase == "drafting"
                        and post_state.loop_instance_id == pre_loop_instance_id
                        and post_state.session_id == sid
                    )
                    guard = {
                        "head_moved": head_moved,
                        "staged_changed": staged_changed,
                        "branch_changed": branch_changed,
                        "post_run_state_mismatch": not state_ok,
                        "binding_mismatches": binding_dims,
                    }
                    if provider.supports_continuation:
                        outcome = result.continuation
                    else:
                        outcome = ContinuationOutcome(
                            capability="unsupported",
                            phase="fresh",
                            failure="error" if not result.ok else "none",
                        )
                    chain_status, action = continuations.classify_continuation(
                        mem_record,
                        guard,
                        avail,
                        outcome,
                    )
                    chain_resumed = chain_status == "resumed"
                    if (
                        mem_record is not None
                        and binding_dims
                        and action
                        in (
                            continuations.CHAIN_ACTION_CLEAR,
                            continuations.CHAIN_ACTION_PERSIST,
                        )
                    ):
                        # A prior chain existed whose binding no longer matches, so
                        # the old conversation was abandoned whether this run
                        # cleared it or replaced it with a freshly captured id.
                        chain_reset_reason = ", ".join(binding_dims)
                    elif (
                        action == continuations.CHAIN_ACTION_CLEAR
                        and mem_record is not None
                    ):
                        if chain_status == "unsupported":
                            chain_reset_reason = "capability:unsupported"
                        else:
                            chain_reset_reason = chain_status

                    try:
                        if action == continuations.CHAIN_ACTION_PERSIST:
                            # Persisting the pre-run expected binding is correct only because a guard-clean
                            # run guarantees pre == post for head, branch, and index (a non-clean run
                            # classifies to CLEAR and never reaches PERSIST or UPDATE). A future edit that
                            # loosens the guard must revisit this: expected would no longer match the
                            # post-run tree.
                            # PERSIST uses the classifier-validated outcome ID as the
                            # sole source of the persisted conversation identity.
                            new_record = continuations.new_record(
                                binding=expected,
                                conversation_id=result.continuation.conversation_id,
                            )
                            continuations.save_record(sid, chain, new_record)
                        elif action == continuations.CHAIN_ACTION_UPDATE:
                            new_record = continuations.new_record(
                                binding=expected,
                                conversation_id=mem_record.conversation_id,
                                created_at=mem_record.created_at,
                                updated_at=datetime.datetime.now(
                                    datetime.UTC,
                                ).isoformat(),
                            )
                            continuations.save_record(sid, chain, new_record)
                        # CHAIN_ACTION_CLEAR is a deliberate no-op because the pre-run tombstone
                        # already ran. CHAIN_ACTION_KEEP is also a deliberate no-op: unavailable
                        # runs never tombstone, and a fresh no-record path has nothing to restore.
                    except Exception as exc:  # noqa: BLE001 - preserve dispatch continuation-store failure contract
                        print(f"note: chain store write failed: {exc}", file=sys.stderr)
                        chain_status = "store_failed"
                        chain_resumed = False
                        chain_reset_reason = "chain store write failed"
                        with contextlib.suppress(Exception):
                            continuations.invalidate(sid, chain)

                envelope = {
                    "seat": seat,
                    "model": result.model,
                    "ok": result.ok,
                    "output": result.output,
                    "error": result.error,
                    "elapsed": result.elapsed,
                    # head_before/after: a commit sha, the "<unborn>" sentinel, or null.
                    "head_before": head_before,
                    "head_after": head_after,
                    "head_moved": head_moved,
                    # staged_before/after: the index tree hash (git write-tree) or null —
                    # NOT a bool; its CHANGE (incl. already-staged -> more-staged) drives
                    # staged_changed.
                    "staged_before": staged_before,
                    "staged_after": staged_after,
                    "staged_changed": staged_changed,
                    # branch_before/after: a branch name, the "<detached HEAD>" sentinel, or null.
                    "branch_before": branch_before,
                    "branch_after": branch_after,
                    "branch_changed": branch_changed,
                    # The formatted recovery warnings for whichever guards fired (empty list
                    # when none did), from the SAME helper the human path renders.
                    "guard_warnings": guard_warnings,
                }
                if chain_requested:
                    envelope["continuation"] = {
                        "chain": chain,
                        "status": chain_status,
                        "resumed": chain_resumed,
                        "reset_reason": chain_reset_reason,
                    }

                if on_settle is not None:
                    on_settle(envelope, result)
                return envelope

            return settlement(finish, result) if settlement is not None else finish()
    except continuations.ContinuationLockError as exc:
        if not lock_acquiring:
            raise
        raise ExecutionBusy(
            f"could not acquire continuation lock for {chain!r}: {exc}"
        ) from exc
    except (continuations.ContinuationError, OSError) as exc:
        if not lock_acquiring:
            raise
        raise ExecutionError(
            f"could not prepare continuation lock for {chain!r}: {exc}"
        ) from exc


def _git_head(repo_dir: str) -> str | None:
    """TRI-STATE HEAD probe in ``repo_dir`` (BLOCKING-2):

    * a real **sha** — a born repo (incl. detached HEAD: ``rev-parse HEAD``
      resolves the checked-out commit);
    * the sentinel ``"<unborn>"`` — a valid work tree with zero commits
      (``rev-parse HEAD`` fails BUT ``rev-parse --is-inside-work-tree`` succeeds);
    * ``None`` — not-a-repo / git error (neither succeeds).
    """
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_dir,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if proc.returncode == 0:
        sha = (proc.stdout or "").strip()
        return sha or None
    # rev-parse HEAD failed — distinguish an unborn repo from a non-repo.
    try:
        inside = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=repo_dir,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if inside.returncode == 0 and (inside.stdout or "").strip() == "true":
        return _UNBORN
    return None


def _git_staged(repo_dir: str) -> str | None:
    """Index-tree-hash snapshot in ``repo_dir`` (BLOCKING-2) — the index's
    CONTENT, not a clean/dirty boolean:

    ``git write-tree`` serializes the current index to a tree object and prints
    the tree SHA WITHOUT touching the index, HEAD, refs, or the working tree, so
    it is safe for a guard. Returns the tree SHA string on success (exit 0), or
    ``None`` on failure / not-a-repo / git error.

    Comparing the before/after tree SHA catches BOTH ``clean -> staged`` AND an
    already-staged index that the seat stages MORE into (``staged -> more
    staged``, where a clean/dirty boolean would read ``True -> True`` and miss
    it). Dispatch explicitly runs on dirty trees, so the already-staged case is
    real.
    """
    try:
        proc = subprocess.run(
            ["git", "write-tree"],
            cwd=repo_dir,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    tree = (proc.stdout or "").strip()
    return tree or None


def _git_branch(repo_dir: str) -> str | None:
    """TRI-STATE branch probe in ``repo_dir`` (mirrors ``_git_head``'s structure):

    * the **branch name** — ``git symbolic-ref --quiet --short HEAD`` succeeds
      (HEAD points symbolically at refs/heads/<branch>; also true in an unborn
      repo before the first commit);
    * the sentinel ``"<detached HEAD>"`` — symbolic-ref FAILS but the repo is a
      valid work tree with a DETACHED HEAD (``rev-parse --is-inside-work-tree``
      succeeds). An IMPOSSIBLE refname (the space) so no real branch can equal
      it; distinct from any branch name and from ``None`` so a
      ``git checkout <sha>`` (main -> detached) fires ``branch_changed=true``
      instead of being silently masked to false;
    * ``None`` — not-a-repo / git error (neither succeeds).

    ``symbolic-ref`` (NOT ``rev-parse --abbrev-ref HEAD``) is load-bearing: on a
    detached HEAD it exits nonzero (so we fall through to the work-tree probe),
    whereas ``rev-parse --abbrev-ref`` returns the literal string ``"HEAD"``
    which would defeat null-safety AND blur detached vs. a branch named HEAD.
    """
    try:
        proc = subprocess.run(
            ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
            cwd=repo_dir,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if proc.returncode == 0:
        name = (proc.stdout or "").strip()
        if name:
            return name
        # symbolic-ref succeeded but printed nothing — fall through to probe.
    # symbolic-ref failed — distinguish a detached HEAD from a non-repo.
    try:
        inside = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=repo_dir,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if inside.returncode == 0 and (inside.stdout or "").strip() == "true":
        return _DETACHED
    return None


def _dispatch_guard_warnings(
    head_moved: bool,
    head_before: str | None,
    head_after: str | None,
    staged_changed: bool,
    branch_changed: bool,
    branch_before: str | None,
    branch_after: str | None,
) -> list[str]:
    """Build the HEAD/staged/branch guard WARNING lines (fixed HEAD->staged->branch
    order). Shared by the success and the failure human-mode paths so a seat that
    FAILED after committing or leaving edits still surfaces the guards, not just the
    error. Text is byte-identical across both paths."""
    lines: list[str] = []
    if head_moved:
        if head_before == _UNBORN:
            lines.append(
                f"WARNING: the dispatched seat moved HEAD {head_before} -> "
                f"{head_after} (it made the repo's FIRST commit against "
                f"instruction; undo with: git update-ref -d HEAD)"
            )
        else:
            lines.append(
                f"WARNING: the dispatched seat moved HEAD {head_before} -> "
                f"{head_after} (it committed against instruction; undo with: "
                f"git reset --soft HEAD@{{1}})"
            )
    # ALWAYS surface a staged change when staged_changed fires: a safety guard must
    # never silently drop a real staged violation. The earlier `not head_moved`
    # suppression was too coarse: a seat that COMMITS and then STAGES additional
    # changes has REAL staged content with head_moved=true, and suppressing the
    # staged line would hide it (the HEAD remedy `git reset --soft HEAD@{1}` leaves
    # those extra staged changes behind). Over-warning is safe for a guard;
    # under-warning is not. To stay non-misleading in BOTH cases, a pure commit
    # (where the index is clean vs the NEW HEAD and `git reset` would be a no-op) AND
    # a real independent stage, the line is DESCRIPTIVE: it reports that the index
    # content changed and points at `git status` to inspect, rather than asserting
    # `git reset` is THE fix (which would be a false no-op promise on a pure commit).
    # The JSON envelope's staged_changed stays computed faithfully.
    if staged_changed:
        lines.append(
            "WARNING: staged/index content changed since before the run — "
            "inspect with `git status`; unstage any unintended changes with "
            "`git reset`"
        )
    if branch_changed:
        # Pick the recovery target for branch_before. When dispatch STARTED on a
        # detached HEAD, branch_before is the (impossible-refname) sentinel: `git
        # checkout <sentinel>` is nonsense; the way back to the original detached
        # state is `git checkout --detach <head_before>` (the ORIGINAL commit sha). A
        # real branch name uses a plain `git checkout`. Null-guard head_before: if
        # the original HEAD probe returned None (a detached HEAD whose _git_head
        # errored while _git_branch succeeded, extremely unlikely but unguarded),
        # fall back to a safe generic hint rather than emitting the literal
        # `--detach None`.
        if branch_before == _DETACHED:
            recover = (
                f"git checkout --detach {head_before}"
                if head_before is not None
                else "re-detach to your original commit"
            )
        else:
            recover = f"git checkout {branch_before}"
        lines.append(
            f"WARNING: the dispatched seat changed branch {branch_before} -> "
            f"{branch_after} against instruction (return with: {recover})"
        )
    return lines
