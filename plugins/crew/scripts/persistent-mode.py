#!/usr/bin/env python3
"""
Claude Crew Persistent Mode Hook
Blocks Stop while a build or measure-twice loop is active.
Generic todo continuation is handled by Claude Code natively.
"""

# --- Version guard: stdlib-only, old-parseable, BEFORE any project import.
# Anything below the marketplace's 3.11 floor is UNSUPPORTED, but must be
# GRACEFUL: allow + loud diagnostic, never a silent crash and never
# functional persistence. Diagnostic channels: stderr + systemMessage (allow-side).
import json
import sys

if sys.version_info < (3, 11):
    _DIAG = (
        "[crew] Stop hook disabled: Python %d.%d is unsupported (crew requires "
        "Python 3.11+). Persistence loops will NOT block — fix the "
        "hook interpreter (e.g. install python3 >= 3.11 on PATH)."
        % sys.version_info[:2]
    )
    print(json.dumps({"systemMessage": _DIAG}))
    print(_DIAG, file=sys.stderr)
    sys.exit(0)

import shlex
from dataclasses import asdict
from pathlib import Path

from models import (
    StopInput,
    HookResult,
    LoopState,
    StateLockError,
    update_state_json,
    read_hook_input,
    record_hook_payload_keys,
    is_verbose,
    override_completion_note,
    truncate,
    utc_now_iso,
    elapsed_minutes,
    effective_count,
    effective_deadline,
    effective_started_at,
    NO_DEADLINE,
    DEFAULT_MAX_STOP_FIRES,
    DEFAULT_MAX_PARKED_FIRES,
    FORCE_EXIT_KEY,
    SCHEMA_VERSION,
    LOAD_CORRUPT,
    LOAD_FUTURE_SCHEMA,
)
from state_discovery import find_session_state_file, is_active_value

# The parsed Stop payload, for the fail-open handler (which runs outside main()
# and would otherwise have nothing to say about the fire that failed).
_HOOK_INPUT: StopInput = StopInput()


def _corrupt_block_message(loop_file: Path, disposition: str) -> str:
    """One-time diagnostic for an unparseable state file (never-choke: the file
    is disposed of so the NEXT Stop allows). ``disposition`` describes what
    actually happened to the file so the message never lies:
    - ``"set_aside"`` — renamed to ``<name>.corrupt`` (the normal path).
    - ``"deleted"``   — rename failed, so it was deleted (unlink fallback).
    - ``"stuck"``     — both rename AND delete failed; the file is untouched.
    """
    if disposition == "set_aside":
        fate = (
            f"It has been set aside as `{loop_file.name}.corrupt`."
        )
    elif disposition == "deleted":
        fate = (
            "It could not be renamed, so the unreadable state file was deleted."
        )
    else:  # "stuck"
        fate = (
            "It could not be set aside or deleted (check permissions) — the "
            "unreadable state file is still in place."
        )
    return (
        f"[Crew - State File Corrupt]\n\n"
        f"The loop state file `{loop_file.name}` could not be parsed (corrupt "
        f"JSON) — any active loop state is lost. {fate} If a loop was active, "
        f"re-init it with `/crew:build` or `/crew:measure-twice`."
    )


def _future_schema_diagnostic(loop_file: Path) -> str:
    """Loud allow-side diagnostic for a newer-crew state file (refuse to
    touch: no rename, no rewrite — a downgrade must not strand a live loop)."""
    return (
        f"[crew] `{loop_file.name}` was written by a newer crew version "
        f"(state schema ahead of this install) — leaving it untouched. Upgrade "
        f"crew or deactivate the loop manually. Allowing stop."
    )


def _handle_load_status(loop_file: Path, status: str):
    """Handle non-OK load statuses. Returns True if the hook emitted output
    and should return; False to continue with the loaded (OK) state."""
    if status == LOAD_CORRUPT:
        # Set the corrupt file aside so the next Stop allows (one-time block).
        corrupt_path = loop_file.with_name(loop_file.name + ".corrupt")
        try:
            loop_file.replace(corrupt_path)
            disposition = "set_aside"
        except OSError:
            # Rename failed — never wedge the user in a permanent block loop.
            # Try to delete the corrupt file so the next Stop allows; if THAT
            # also fails, allow this Stop (fail-open) with the diagnostic on the
            # allow side rather than blocking forever with no path to recovery.
            try:
                loop_file.unlink()
                disposition = "deleted"
            except OSError:
                print(HookResult.allow_with_diagnostic(_corrupt_block_message(loop_file, "stuck")).to_json())
                return True
        print(HookResult.block(_corrupt_block_message(loop_file, disposition)).to_json())
        return True
    if status == LOAD_FUTURE_SCHEMA:
        # Refuse to touch: bytes untouched, loud diagnostic, allow stop.
        print(HookResult.allow_with_diagnostic(_future_schema_diagnostic(loop_file)).to_json())
        return True
    return False


def _normalize_bounds(state) -> None:
    """Coerce the hook-owned bound fields in place before anything reads them.

    Same threat model the deadline already had: the file is hand-editable, and a
    non-int counter would raise straight into the fail-open handler, so the fire
    that trips a tampered bound is the one where the bound stops enforcing. The
    coerced values are what the fire then saves, so a garbage counter self-heals
    into a real int rather than raising again next fire.

    `started_at` gets the same treatment for a sharper reason: a bad value there
    does not raise, it DELETES the wall clock (unparseable reads as "no elapsed
    time known", future-dated reads as negative elapsed), so the only cost ceiling
    in the system can be switched off with valid JSON. Restamping bounds the loop
    from this fire forward instead of never.
    """
    state.stop_fires = effective_count(state.stop_fires, 0)
    state.max_stop_fires = effective_count(state.max_stop_fires, DEFAULT_MAX_STOP_FIRES)
    state.parked_fires = effective_count(state.parked_fires, 0)
    state.max_parked_fires = effective_count(
        state.max_parked_fires, DEFAULT_MAX_PARKED_FIRES)
    state.started_at = effective_started_at(state.started_at)[0]


def _is_completed_phase(phase) -> bool:
    """Has the loop already recorded its completing verdict?

    A completing verdict leaves `phase=done, active=true`: `record-verdict` sets
    the phase and `deactivate` stamps the outcome, so `done` is the handoff
    window between the two, not an unfinished loop. Read defensively like the
    counters: a garbage value answers False rather than raising.
    """
    return (phase or "drafting") == "done"


def _termination_reason(state):
    """Why this loop must stop NOW, or None to keep going.

    Both bounds are hook-owned (see ``AGENT_SETTABLE``) and read through the
    defensive coercions above, never raw off disk. Neither is a stall timer: a
    single panel seat can take minutes, and any late-landing seat would reset a
    stall clock forever.
    """
    if state.stop_fires >= state.max_stop_fires:
        return (
            f"livelock circuit breaker: {state.stop_fires} Stop fires without "
            f"the loop finishing (limit {state.max_stop_fires})"
        )
    deadline = effective_deadline(
        state.deadline_minutes,
        opted_out=getattr(state, "no_deadline", False) is True)
    if deadline == NO_DEADLINE:
        # The operator opted the clock off at init (both marker halves present);
        # the stop-fires cap remains the bound.
        return None
    minutes = elapsed_minutes(state.started_at)
    if minutes is not None and minutes >= deadline:
        return (
            f"deadline reached: {minutes:.0f} minutes elapsed "
            f"(limit {deadline})"
        )
    return None


def _elapsed_note(state) -> str:
    """One-line budget + position line for the nudge, so a runaway loop is VISIBLE.

    The hook OWNS neither phase nor round (the review verbs write both), it only
    reports them: a nudge that says "continue" without saying where the loop
    stands leaves the orchestrator to guess which verb is legal next. Both are
    read defensively, like the counters: a garbage value renders, never raises.
    """
    minutes = elapsed_minutes(state.started_at)
    deadline = effective_deadline(
        state.deadline_minutes,
        opted_out=getattr(state, "no_deadline", False) is True)
    limit = "none" if deadline == NO_DEADLINE else str(deadline)
    clock = f"{minutes:.0f}/{limit} min" if minutes is not None else "unknown"
    phase = getattr(state, "phase", "") or "drafting"
    review_round = effective_count(getattr(state, "revision_round", 0), 0)
    return (
        f"Elapsed: {clock} · stop fires: {state.stop_fires}/{state.max_stop_fires}"
        f" · phase={phase} round={review_round}"
    )


def _hook_owned(data: dict, state) -> dict:
    """Stamp the keys the Stop hook owns onto the dict on disk, in place.

    The hook writes ONLY these. A whole-state write (`asdict(state)`) would also
    drop every key this install does not model and revert whatever another writer
    landed between the hook's load and its save: the mirror image of the
    stale-snapshot hazard the CLI's field-level write closes. Two writers are in
    that window. `crew state set` lands `plan_file` (its whole allowlist), and the
    review verbs land the review record (`last_verdict`, `phase`, the frozen run
    identity). Neither is the hook's to write back.
    """
    # Adopting a legacy file stamps the version without backfilling `task`/`loop`:
    # a `schema: 3` file on disk is therefore NOT a guarantee of the schema-3 key
    # shape. Reads survive because every task read coalesces the legacy keys, so a
    # writer must coalesce too rather than trust the stamp for key presence.
    data["schema"] = SCHEMA_VERSION
    data["session_id"] = state.session_id
    data["started_at"] = state.started_at
    data["max_stop_fires"] = state.max_stop_fires
    data["max_parked_fires"] = state.max_parked_fires
    return data


def _persist_fires(loop_file: Path, state, stop_delta: int = 0, parked: str = "keep",
                   clear_awaiting: bool = False) -> None:
    """Persist this fire's counters under the shared state lock.

    The counters are recomputed from the dict read INSIDE the lock, not from the
    snapshot this process loaded, so a bump another writer landed in that window is
    added to, never overwritten. `parked` is one of keep / bump / reset.
    `clear_awaiting` turns off the human-wait flag on a nudge (the loop is being
    told to work, so it is no longer waiting); the allow path leaves it set.
    """
    def mutate(data: dict) -> dict:
        _hook_owned(data, state)
        stop_fires = effective_count(data.get("stop_fires"), 0) + stop_delta
        parked_fires = effective_count(data.get("parked_fires"), 0)
        if parked == "bump":
            parked_fires += 1
        elif parked == "reset":
            parked_fires = 0
        data["stop_fires"] = stop_fires
        data["parked_fires"] = parked_fires
        if clear_awaiting:
            data["awaiting_input"] = False
        # Report what was PERSISTED (the nudge prints these), not the pre-lock read.
        state.stop_fires = stop_fires
        state.parked_fires = parked_fires
        return data

    update_state_json(loop_file, mutate, default=asdict(state))


def _force_exit(loop_file: Path, state, reason: str) -> bool:
    """Turn the loop off the way `crew state deactivate` does, stamping
    `completed_at` + `reason` alongside `active=False`. Without them a
    force-exited loop is indistinguishable from a clean finish on later
    inspection. This is the ONE writer of `exit_kind="force_exit"` (the `reason`
    alongside it says WHICH bound tripped). The `force_exit` marker is what `crew
    state init` reads to refuse a fresh loop over a tripped bound without
    `--force`.

    INVARIANT: this never relabels a loop that has already reached a terminal or
    completing state, so there is ONE condition here, not a growing list of them.

    Such a loop is past the hook's reach in two shapes, and both are the same
    fact: it is already inactive (a `deactivate` or a terminal FAILED won the race
    to end it, and restamping `exit_kind="force_exit"` over that would make the
    audit metadata describe an ending that never happened), or it sits at
    `phase=done` (it finished, and stamping a safety failure over signed-off work
    would be a lie; `deactivate` is the one step left, which the guidance routes
    to). The pre-lock read cannot settle either one, since either can land in its
    own transaction before this one takes the lock, so the re-check happens INSIDE
    the lock on the on-disk bytes, via the shared truthiness rule the rest of the
    state surface uses.

    Returns True only when this call actually deactivated the loop, so the caller
    announces a deactivation only when one happened: the no-op branch leaves the
    bytes alone, and a "Loop deactivated" banner over it reports a write that never
    landed.
    """
    transitioned = False

    def mutate(data: dict) -> dict:
        nonlocal transitioned
        if (not is_active_value(data.get("active", False))
                or _is_completed_phase(data.get("phase", ""))):
            return data
        _hook_owned(data, state)
        data["stop_fires"] = state.stop_fires
        data["parked_fires"] = state.parked_fires
        data["active"] = False
        data["completed_at"] = utc_now_iso()
        data["reason"] = reason
        data["exit_kind"] = "force_exit"
        data[FORCE_EXIT_KEY] = True
        transitioned = True
        return data

    update_state_json(loop_file, mutate, default=asdict(state))
    return transitioned


def _done_body(loop, session_flag, closing, cancel_cmd, state) -> str:
    """The phase=done guidance: ONE remaining step (deactivate), described
    honestly for how the completing verdict was recorded.

    A completion whose `last_verdict_overrides` is non-empty was recorded on a
    human's `--force` over one or more advisories, NOT a clean sign-off. Keyed on
    the stamp so the nudge and the session-start banner tell the same story; a
    clean completion (empty overrides) reads exactly as a plain approval, and its
    deactivate reason stays "Panel approved".
    """
    note = override_completion_note(getattr(state, "last_verdict_overrides", None))
    if note:
        opening = (
            f"This loop's completing verdict was {note} (phase=done), so the "
            f"review verbs are finished with this loop: begin-review and "
            f"record-verdict both refuse from here."
        )
        reason = "Completion forced over advisories"
    else:
        opening = (
            "The panel's completing verdict is already recorded (phase=done), so "
            "the review verbs are finished with this loop: begin-review and "
            "record-verdict both refuse from here."
        )
        reason = "Panel approved"
    return f"""{opening} ONE step remains:

"${{CLAUDE_PLUGIN_ROOT}}/crew" state deactivate {loop} --reason "{reason}"{session_flag}

{closing} To exit without that: `{cancel_cmd}`"""


def _handle_loop(loop_file, state, parked, banner, task_text, detail_lines, recipe,
                 cancel_cmd, done_body=""):
    """Shared Stop handling for both loops. Emits output; returns True if handled.

    The hook writes ONLY what it can honestly observe: `stop_fires`,
    `parked_fires`, the `started_at` stamp, and the deactivation on a tripped
    bound. It counts no revision rounds, because a Stop fire is not a round: an
    earlier version conflated them, and the loop then reset its own counter to
    escape a limit it kept hitting for merely waiting.
    """
    _normalize_bounds(state)
    completed = _is_completed_phase(getattr(state, "phase", ""))
    # The bounds bound WORK, and a completed loop has none left: it outran them,
    # so it routes to its one legal remaining step (deactivate, below) rather than
    # tripping a limit that would record signed-off work as a safety failure. A
    # loop still drafting or reviewing faces both bounds exactly as before.
    reason = None if completed else _termination_reason(state)
    if reason:
        if _force_exit(loop_file, state, reason):
            print(HookResult.block(
                f"""[{banner} - Safety Limit Reached]

Loop deactivated: {reason}.

Do NOT re-init the loop. Report where the work actually stands: what completed,
what did not, and what you were waiting on."""
            ).to_json())
            return True
        # The bound tripped against the pre-lock read, but the loop reached a
        # terminal or completing state before the lock took, so nothing was
        # deactivated. Announcing one would report a write that never landed; the
        # real remaining step is the one a completed loop already routes to.
        completed = True

    # A WAIT, not a quit, in TWO shapes: `parked` (the harness has background work
    # in flight) OR `awaiting_input` (the loop is blocked on the human: an ok=false
    # executor stop, an exit-3 --force advisory, an AskUserQuestion, with nothing
    # in flight, so `parked` is false). The old hook knew only the first, so a loop
    # legitimately waiting on a human was blocked on EVERY turn-end and the agent
    # was dragged back turn after turn: the nudge livelock. Both waits ALLOW the
    # Stop under the SAME max_parked_fires cap, so a forgotten flag cannot disable
    # the loop, and the wall-clock deadline (checked above) still bounds it.
    waiting = parked or state.awaiting_input
    if waiting:
        if state.parked_fires < state.max_parked_fires:
            # Still waiting (launched work in flight, or the human has not replied).
            # ALLOW it: the harness re-invokes the session when the work completes,
            # and the human's next message re-invokes it when they reply. Blocking
            # here is what manufactured the busy-wait: the agent was forced to take a
            # turn with nothing to do, so it polled, which burned the counter and
            # flooded the context until compaction wiped its memory of the panel.
            _persist_fires(loop_file, state, parked="bump")
            print(HookResult.allow().to_json())
            return True
        # Past the cap, this Stop is treated as an ordinary one (nudge + a
        # stop_fire). The park signal cannot tell crew's own seats from an
        # unrelated long-lived shell (a dev server, a `tail -f`), and such a
        # shell would otherwise park every Stop and silently disable the loop:
        # no nudge, no bound, no diagnostic. The counter RESETS here too: the
        # nudge forces a working turn, which ends the unbroken run of parks the
        # cap exists to bound. A kept counter turned this one-time backstop
        # into a permanent penalty; in a session where a long-lived shell
        # parked EVERY turn-end, the non-parked reset below was unreachable,
        # so legitimate panel waits exhausted the cap cumulatively and every
        # later turn-end burned a stop_fire. The next cap-length run of parks
        # still costs a nudge + a stop_fire, and the wall-clock deadline still
        # bounds a loop that only ever parks (termination beats parking).
        # `clear_awaiting`: if a STUCK awaiting_input drove this run to the cap (the
        # agent forgot to clear it after the human replied), the nudge just earned
        # clears it, so the next yield blocks again instead of riding a stale flag.
        _persist_fires(loop_file, state, stop_delta=1, parked="reset", clear_awaiting=True)
    else:
        # parked_fires counts CONSECUTIVE parks: a fire with nothing in flight
        # means the session came back and did work, so the wait was real. (Any
        # BLOCKING fire resets the run; this branch and the past-cap nudge
        # above are the two.)
        _persist_fires(loop_file, state, stop_delta=1, parked="reset")

    # From `done` the guidance is phase-specific and REPLACES both bodies: the
    # verdict is already recorded, so `begin-review` and `record-verdict` would
    # both refuse, and a loop nudged onto them cannot progress while the hook
    # keeps blocking. The terse wait guard is wrong here too (a recorded verdict
    # means no seat is in flight), so `done` overrides verbosity rather than
    # sitting behind it.
    if done_body and completed:
        body = done_body
    # Otherwise the full recipe is CREW_VERBOSE-only. It used to also print on the
    # first fire, but the first fire is not special: the loop parks and re-fires
    # many times per round, so that only bought a wall of text at the least useful
    # moment. The one line that must survive terse mode is the wait guard:
    # re-running a panel mid-flight deletes the seats that already landed.
    elif is_verbose():
        body = recipe
    else:
        body = (
            f"Waiting on seats? Just wait. Do NOT re-run the panel or clear "
            f"landed seat files. Exit: `{cancel_cmd}`"
        )

    # Assembled line by line so a loop with no detail lines (the build loop) does
    # not render an empty one between the task and the body.
    lines = [f"[{banner}]", _elapsed_note(state), "", f"Task: {task_text}"]
    lines.extend(detail_lines)
    lines.append(body)
    print(HookResult.block("\n".join(lines)).to_json())
    return True


def main():
    global _HOOK_INPUT
    data = read_hook_input()
    record_hook_payload_keys("Stop", data)
    hook_input = StopInput.from_dict(data)
    _HOOK_INPUT = hook_input
    directory = hook_input.directory_path
    session_id = hook_input.session_id

    crew_dir = directory / ".crew"

    # Rendered inside copy-pasteable shell commands: only emit the flag when
    # there is a value (no trailing bare `--session-id`), and shell-quote it.
    session_flag = f" --session-id {shlex.quote(session_id)}" if session_id else ""

    # Priority 1: Build Loop (session-scoped lookup)
    loop_file = find_session_state_file(crew_dir, "build-state", session_id)
    if loop_file:
        loop_state, status = LoopState.load_with_status(loop_file)
        if _handle_load_status(loop_file, status):
            return

        if loop_state.active:
            # adoption stamp: claim an unowned legacy file for this session
            # before the save (the fire already saves, zero extra writes).
            # Closes the co-adoption window at first touch. A schema-1 file's
            # missing started_at is stamped by _normalize_bounds, with every other
            # unusable value of that field.
            if not loop_state.session_id and session_id:
                loop_state.session_id = session_id
            _handle_loop(
                loop_file,
                loop_state,
                hook_input.is_parked,
                banner="Build Loop",
                task_text=truncate(loop_state.task, 120),
                detail_lines=[],
                recipe=f"""
Continue working. When complete, verify via the multi-model panel
(/crew:build Step 2):
1. "${{CLAUDE_PLUGIN_ROOT}}/crew" review-prep working-tree{session_flag}
2. "${{CLAUDE_PLUGIN_ROOT}}/crew" state begin-review bl{session_flag} (freeze the run identity BEFORE any seat runs; leave the tree alone until the verdict is recorded)
3. Fan out the PENDING seats from the prep JSON (a resumed run relaunches only those), wait for every seat, collect the FULL roster, synthesize
4. Choose the verdict the digest supports. If its quorum header reads NOT MET, a COMPLETING verdict (APPROVED, or REVISE --minor-only) needs the user's explicit --force at step 5: quorum gates sign-off only. Where the usable seats found real blocking issues, choose a plain REVISE (or REJECT); otherwise relaunch the pending seats (same --run-id) or surface the shortfall, then re-collect and choose from the new digest
5. Record that ONE chosen verdict: "${{CLAUDE_PLUGIN_ROOT}}/crew" state record-verdict bl <APPROVED|REVISE [--minor-only]|REJECT|FAILED>{session_flag} (the ONE place any verdict is recorded, whichever branch chose it; FAILED only when no seat returned anything usable). A completing verdict may exit 3 (a completion advisory tripped: short quorum, a drifted tree): it recorded NOTHING. Do NOT add --force yourself. Surface the advisory to the user, say what --force would do, and WAIT; re-run with --force ONLY on the user's explicit say-so (it is the human's authorization, never yours to originate)
6. If that verdict completed the loop (APPROVED, or REVISE --minor-only), it printed phase=done: deactivate the loop and summarize
7. If REVISE with [BLOCKING] issues, fix and re-verify from step 1

When re-delegating the implement/revision step, route it through the CONFIGURED
executor (re-resolve via `crew build-executor{session_flag}` / the shared executor-step block in
/crew:build), NOT a bare `Task(crew:executor)`: substituting Claude for the seat the
user configured defeats the never-silently-fall-back guarantee.

If you are WAITING on seats that are still running, just wait. Do NOT re-run
the panel and do NOT clear seat files that already landed.

To exit early: `/crew:cancel-build`""",
                done_body=_done_body(
                    "bl", session_flag,
                    "Then summarize what was accomplished.",
                    "/crew:cancel-build", loop_state),
                cancel_cmd="/crew:cancel-build",
            )
            return

    # Priority 2: Measure-Twice Loop (session-scoped lookup)
    measure_file = find_session_state_file(crew_dir, "measure-twice-state", session_id)
    if measure_file:
        measure_state, status = LoopState.load_with_status(measure_file)
        if _handle_load_status(measure_file, status):
            return

        if measure_state.active:
            # adoption stamp (see build loop above).
            if not measure_state.session_id and session_id:
                measure_state.session_id = session_id
            _handle_loop(
                measure_file,
                measure_state,
                hook_input.is_parked,
                banner="Measure-Twice Loop",
                task_text=truncate(measure_state.task, 120),
                detail_lines=[f"Plan: {measure_state.plan_file}"],
                recipe=f"""
Continue refining the plan. Verify via the multi-model panel
(/crew:measure-twice Phase 3):
1. If you just received panel feedback, revise the plan to address [BLOCKING] issues
2. "${{CLAUDE_PLUGIN_ROOT}}/crew" review-prep {shlex.quote(measure_state.plan_file)}{session_flag}
3. "${{CLAUDE_PLUGIN_ROOT}}/crew" state begin-review mt{session_flag} (freeze the run identity BEFORE any seat runs; leave the plan alone until the verdict is recorded)
4. Fan out the PENDING seats from the prep JSON (a resumed run relaunches only those), wait for every seat, collect the FULL roster, synthesize
5. Choose the verdict the digest supports. If its quorum header reads NOT MET, a COMPLETING verdict (APPROVED, or REVISE --minor-only) needs the user's explicit --force at step 6: quorum gates sign-off only. Where the usable seats found real blocking issues, choose a plain REVISE (or REJECT); otherwise relaunch the pending seats (same --run-id) or surface the shortfall, then re-collect and choose from the new digest
6. Record that ONE chosen verdict: "${{CLAUDE_PLUGIN_ROOT}}/crew" state record-verdict mt <APPROVED|REVISE [--minor-only]|REJECT|FAILED>{session_flag} (the ONE place any verdict is recorded, whichever branch chose it; FAILED only when no seat returned anything usable). A completing verdict may exit 3 (a completion advisory tripped: short quorum, a drifted plan): it recorded NOTHING. Do NOT add --force yourself. Surface the advisory to the user, say what --force would do, and WAIT; re-run with --force ONLY on the user's explicit say-so (it is the human's authorization, never yours to originate)
7. If that verdict completed the loop (APPROVED, or REVISE --minor-only), it printed phase=done: deactivate the loop and present the final plan

If you are WAITING on seats that are still running, just wait. Do NOT re-run
the panel and do NOT clear seat files that already landed.

To exit early: `/crew:cancel-measure-twice`""",
                done_body=_done_body(
                    "mt", session_flag,
                    "Then present the final plan.",
                    "/crew:cancel-measure-twice", measure_state),
                cancel_cmd="/crew:cancel-measure-twice",
            )
            return

    # No active loop — allow stop
    print(HookResult.allow().to_json())


def _fail_open(diag: str) -> None:
    """Allow the stop, LOUDLY (stderr + systemMessage), never silently.

    `stop_hook_active` is the one fact the payload carries about the failing fire:
    it says whether this Stop was already being continued by a hook, i.e. whether
    a blocked loop just lost its enforcement or nothing was being held anyway.
    """
    if _HOOK_INPUT.stop_hook_active:
        diag += " This Stop was already being continued by a hook (a loop was blocking)."
    print(HookResult.allow_with_diagnostic(diag).to_json())
    print(diag, file=sys.stderr)
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except StateLockError as exc:
        # The state file's lock could not be taken, so this fire cannot write.
        # Blocking without a write is what wedges a loop: it would re-nudge
        # forever with the counters frozen, so the bound that ends it never
        # arrives. Allow, loudly.
        _fail_open(
            "[crew] Stop hook could not lock the loop state file (%s), so it cannot "
            "write: allowing stop. This loop's bounds are not enforced on this fire."
            % exc
        )
    except Exception as exc:  # noqa: BLE001. Stop is architecturally fail-open;
        # make the failure LOUD (stderr + systemMessage), never silent.
        _fail_open(
            "[crew] Stop hook crashed (%s: %s) — allowing stop. An active "
            "persistence loop may not be enforced this fire."
            % (exc.__class__.__name__, exc)
        )
