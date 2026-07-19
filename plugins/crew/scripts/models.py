#!/usr/bin/env python3
"""
Data models for Claude Crew hooks.

All JSON structures used by hooks are defined here as dataclasses
for type safety, validation, and self-documentation.
"""

from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import ClassVar, Optional, Any
import contextlib
import json
import os
import sys
import tempfile
import time

from state_discovery import is_active_value, crew_base

try:
    import fcntl
except ImportError:  # non-POSIX: no interprocess lock primitive available
    fcntl = None


# =============================================================================
# Hook Input Models
# =============================================================================

@dataclass
class WebFetchInput:
    """Input structure for WebFetch tool calls (PreToolUse hook)."""
    url: str = ""
    prompt: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "WebFetchInput":
        tool_input = data.get("tool_input", {})
        return cls(
            url=tool_input.get("url", ""),
            prompt=tool_input.get("prompt", ""),
        )


@dataclass
class PreToolUseInput:
    """Input structure for PreToolUse hooks."""
    tool_name: str = ""
    tool_input: dict = field(default_factory=dict)
    session_id: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "PreToolUseInput":
        return cls(
            tool_name=data.get("tool_name", ""),
            tool_input=data.get("tool_input", {}),
            session_id=data.get("session_id", data.get("sessionId", "")),
        )

    @property
    def url(self) -> str:
        """Convenience accessor for WebFetch URL."""
        return self.tool_input.get("url", "")


def _get_project_dir() -> str:
    """Get the project directory via the ONE shared resolver.

    Delegates to ``crew_base`` so the state layer resolves `.crew` through the
    SAME function the review engine and the session-start sweeps do (no
    models-vs-crew_base pair that can pick different trees). A hook runs with
    CLAUDE_PROJECT_DIR set and its cwd AT the project root, so the resolver never
    needs the payload's `directory`/`cwd`.
    """
    return str(crew_base())


@dataclass
class SessionStartInput:
    """Input structure for SessionStart hooks."""
    directory: str = ""
    session_id: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "SessionStartInput":
        return cls(
            directory=_get_project_dir(),
            session_id=data.get("session_id", data.get("sessionId", "")),
        )

    @property
    def directory_path(self) -> Path:
        return Path(self.directory)


# Statuses that mean a background task is DONE. Anything else, including an
# absent or unrecognized status, is treated as still running: the safe default is
# to keep waiting, never to assume work finished and let an unverified loop stop.
TERMINAL_TASK_STATUSES = frozenset({"completed", "failed", "killed", "cancelled", "done", "error"})


def _task_in_flight(task) -> bool:
    """True when a background_tasks entry is not known to have finished."""
    if not isinstance(task, dict):
        # A shape we do not understand is not evidence that work finished.
        return True
    status = task.get("status")
    if not isinstance(status, str):
        return True
    return status.strip().lower() not in TERMINAL_TASK_STATUSES


@dataclass
class StopInput:
    """Input structure for Stop hooks."""
    directory: str = ""
    session_id: str = ""
    # Harness-tracked work the session launched and has not consumed yet
    # (background shells, async subagents). Verified present on the live Stop
    # payload. A non-empty list is the ONE honest signal that separates "parked
    # waiting on my own work" from "trying to quit". Without it the hook has to
    # guess, and guessing wrong is what drove the loop to poll.
    background_tasks: list = field(default_factory=list)
    # The other documented wake signal on the Stop payload: a scheduled wake-up
    # is a wait, not a quit, so it parks the same way background_tasks does.
    session_crons: list = field(default_factory=list)
    # Set by Claude Code when THIS Stop was already continued by a hook. It is NOT
    # an enforcement input (crew bounds the loop with its own stop_fires): the Stop
    # hook renders it into the fail-open diagnostic, where it is the one fact that
    # says whether the fire that failed was already inside a blocked loop.
    stop_hook_active: bool = False

    @classmethod
    def from_dict(cls, data: dict) -> "StopInput":
        tasks = data.get("background_tasks")
        crons = data.get("session_crons")
        return cls(
            directory=_get_project_dir(),
            session_id=data.get("session_id", data.get("sessionId", "")),
            # A non-list (older or future harness) reads as "no signal" rather
            # than crashing the hook.
            background_tasks=tasks if isinstance(tasks, list) else [],
            session_crons=crons if isinstance(crons, list) else [],
            stop_hook_active=bool(data.get("stop_hook_active", False)),
        )

    @property
    def is_parked(self) -> bool:
        """True when the session still has work in flight it launched itself.

        Counts only tasks that are STILL RUNNING. A probe of the live payload
        shows the harness already prunes finished work (a completed shell and a
        completed subagent were both absent while a running shell and a running
        subagent were listed with `status: "running"`), so today this filter is a
        no-op. It is here because the alternative is not survivable: if a future
        harness kept finished tasks listed, every Stop after one panel fan-out
        would read as parked, and a parked Stop is ALLOWED, so the panel-approval
        gate would switch itself off with no error and no diagnostic.

        An entry whose status is unknown or absent counts as IN FLIGHT, so an
        unrecognized payload degrades to parking (a needless wait) rather than to
        waving work through unverified.
        """
        live = [t for t in self.background_tasks if _task_in_flight(t)]
        return bool(live) or bool(self.session_crons)

    @property
    def directory_path(self) -> Path:
        return Path(self.directory)


# =============================================================================
# Hook Output Models
# =============================================================================

@dataclass
class HookResult:
    """Output structure for all hooks."""
    continue_: bool = True  # 'continue' is a Python keyword
    message: Optional[str] = None
    reason: Optional[str] = None
    system_message: Optional[str] = None  # user-visible diagnostic (allow-side)

    def to_json(self) -> str:
        """Serialize to JSON for hook output.

        Each hook output uses a single JSON shape (dual-emit here is UNSAFE):

        - Block: ``{"decision": "block", "reason": msg}`` is the load-bearing
          shape. The legacy ``{"continue": false, ...}`` is INERT for blocking
          a Stop: Claude Code honors it as a HARD-STOP directive (the child
          session ends at the first stop attempt with
          ``terminal_reason=stop_hook_prevented``, the loop never resuming).
          Because ``continue: false`` demonstrably forces termination, it must
          NOT be dual-emitted alongside ``decision: block``.
        - Allow: ``{}`` (benign empty object); asserts nothing about the
          ambiguous ``continue`` field.
        """
        result: dict[str, Any] = {}
        if not self.continue_:
            result["decision"] = "block"
            if self.reason is not None:
                result["reason"] = self.reason
        if self.message is not None:
            result["message"] = self.message
        if self.system_message is not None:
            # "systemMessage" on an allow payload surfaces to the user: Claude
            # Code parses it into a first-class hook_system_message transcript
            # attachment, the loud-diagnostic carrier for fail-open hook paths.
            result["systemMessage"] = self.system_message
        return json.dumps(result)

    @classmethod
    def allow(cls, message: Optional[str] = None) -> "HookResult":
        """Create a result that allows the action to continue."""
        return cls(continue_=True, message=message)

    @classmethod
    def allow_with_diagnostic(cls, diagnostic: str) -> "HookResult":
        """Allow, but surface a loud user-visible diagnostic (systemMessage)."""
        return cls(continue_=True, system_message=diagnostic)

    @classmethod
    def block(cls, reason: str) -> "HookResult":
        """Create a result that blocks the action."""
        return cls(continue_=False, reason=reason)


@dataclass
class SessionStartResult:
    """Output structure specifically for SessionStart hooks.

    SessionStart hooks use hookSpecificOutput.additionalContext to pass
    context to Claude, NOT the message field (which is for PermissionRequest).
    """
    additional_context: Optional[str] = None

    def to_json(self) -> str:
        """Serialize to JSON for hook output."""
        result: dict[str, Any] = {}
        if self.additional_context:
            result["hookSpecificOutput"] = {
                "hookEventName": "SessionStart",
                "additionalContext": self.additional_context,
            }
        return json.dumps(result)

    @classmethod
    def with_context(cls, context: str) -> "SessionStartResult":
        """Create a result with additional context for Claude."""
        return cls(additional_context=context)


# =============================================================================
# State Models
# =============================================================================

# Current on-disk state-file schema. `load` treats an absent `schema` key
# as 1 (legacy files keep working); a HIGHER value than this is a
# newer-crew file that must be REFUSED, never rewritten (see LOAD_FUTURE_SCHEMA).
# 2 added the hook-owned termination fields (stop_fires / started_at); a schema-1
# file loads with their defaults, and the hook stamps started_at on first fire.
# 3 unified the two loop dataclasses into ONE LoopState: `task` replaces the
# per-loop `prompt`/`task_description` (reads coalesce the legacy keys, see
# coalesce_task), a `loop` stamp names which loop owns the file, and the
# review-gate fields (phase / run identity / exit_kind) join the shape.
# `completion_promise` is gone. The bump is what makes an OLDER install refuse
# to touch a live schema-3 loop instead of silently dropping those fields on a
# whole-state write.
SCHEMA_VERSION = 3

# Termination bounds, both hook-owned. They are deliberately generous: a bound
# that trips during legitimate work teaches the agent to route around it, which
# is how the counter-reset livelock started.
#
# stop_fires is the livelock circuit breaker: it counts what a Stop hook can
# honestly observe (its own fires). A real panel wait costs tens of Stop fires,
# so this is set well above any honest loop.
DEFAULT_MAX_STOP_FIRES = 150

# The absolute wall clock, and the only cost ceiling in the system: every other
# bound is denominated in a unit the agent controls. Deliberately NOT a stall
# timer: a single seat legitimately takes minutes, and any late seat would reset
# a stall clock forever. Sized for a remote or intermittently-attended operator:
# a multi-round build with a 7-seat panel plus an hour-plus approval latency
# must fit inside one window. Overridable per repo/user via
# `[tuning].deadline_minutes`.
DEFAULT_DEADLINE_MINUTES = 240

# The policy ceiling on that wall clock. `init` is invoked BY the agent, so the
# deadline it may request is bounded on BOTH ends: a bound the bounded thing can
# widen (or delete with a negative) is not a bound.
MAX_DEADLINE_MINUTES = 1440

# The deliberate no-deadline opt-out, accepted ONLY from
# [tuning].deadline_minutes at init time. Config is the operator's channel; the
# CLI flag REJECTS 0 because init is invoked by the agent (the loop commands
# template that call), and a bound the bounded thing can delete is not a bound.
# On disk the opt-out
# is a TWO-field marker that only init writes: deadline_minutes == 0 AND
# no_deadline == true. A lone 0 (a hand-edit, bitrot, or a pre-upgrade file,
# where 0 always read as the bounded default) still reads as the bounded
# default, so no accident and no upgrade can delete the ceiling; the stop-fires
# cap keeps bounding the loop even with the clock off.
NO_DEADLINE = 0

# Consecutive parked Stops (see StopInput.is_parked) allowed before the hook
# nudges anyway. Generous on purpose: a genuine seat wait costs one or two parked
# fires per round, so honest waiting stays effectively free. Its job is the case
# the park signal cannot distinguish: an unrelated background shell that never
# exits would otherwise park every Stop and silently disable the loop.
DEFAULT_MAX_PARKED_FIRES = 20

# Stamped by the Stop hook next to `completed_at`/`reason` when a termination
# bound trips. `crew state init` reads it to tell a force-exit apart from a clean
# `deactivate`, so re-initing straight over a tripped safety limit takes an
# explicit `--force` instead of being the default path.
FORCE_EXIT_KEY = "force_exit"


def utc_now_iso() -> str:
    """Timezone-aware UTC timestamp for the state files' clock fields."""
    return datetime.now(timezone.utc).isoformat()


def effective_count(value, default: int) -> int:
    """A hook-owned counter/bound read defensively off disk.

    The state file is hand-editable, so every reader of these ints must survive a
    non-int or negative value: an unguarded comparison raises into the Stop hook's
    fail-open handler, which ALLOWS the stop, i.e. the circuit breaker quietly
    stops enforcing on exactly the fire someone tampered with. Fall back to the
    caller's default (never to "unbounded").
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return default
    return value


def effective_deadline(value, opted_out=False) -> int:
    """The wall clock actually enforced, in minutes (same threat model as above).
    Returns NO_DEADLINE (0) for the deliberate opt-out; callers treat 0 as
    "clock off" and rely on the stop-fires cap.

    The opt-out requires BOTH halves of the marker init writes: value exactly 0
    AND the state's no_deadline flag (passed as ``opted_out``). A lone 0 reads
    as the bounded default, exactly as it did before the opt-out existed: under
    the old semantics 0 was corruption-shaped and bounded, so honoring it alone
    would let a pre-upgrade or hand-edited file silently delete the ceiling on
    upgrade. A negative, non-integer, or too-large value stays bounded/clamped
    as before. Single-sourced here so the Stop hook, `crew state show`, and the
    SessionStart banner can never report a different bound than the one that
    ends the loop.
    """
    if (opted_out is True and value == NO_DEADLINE
            and isinstance(value, int) and not isinstance(value, bool)):
        return NO_DEADLINE
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return DEFAULT_DEADLINE_MINUTES
    return min(value, MAX_DEADLINE_MINUTES)


def parse_iso(value: str):
    """Parse an ISO timestamp written by utc_now_iso, or None if unusable.

    Never raises: a hand-edited or truncated timestamp must not wedge the Stop
    hook, it just means "no wall-clock bound available this fire".
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    # A naive timestamp (hand-edited, or written by an older build) is read as
    # UTC rather than rejected, so the deadline still bounds the loop.
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def elapsed_minutes(started_at: str):
    """Minutes since `started_at`, or None when it can't be determined."""
    started = parse_iso(started_at)
    if started is None:
        return None
    delta = datetime.now(timezone.utc) - started
    return delta.total_seconds() / 60.0


# How far ahead of this machine's clock a `started_at` may sit before it is read
# as bogus rather than as skew. A stamp written seconds ago on a slightly-ahead
# clock is normal; minutes ahead is not a clock, it is a value nothing should
# trust with the only cost ceiling in the system.
CLOCK_SKEW_TOLERANCE_MINUTES = 2


def effective_started_at(value):
    """The wall clock's start stamp, read defensively. Returns ``(stamp, repaired)``.

    Same threat model as ``effective_count``, and the same self-heal: the state
    file is hand-editable, and here a bad value does not raise, it silently
    DELETES the deadline. An unparseable or wrong-typed stamp makes
    ``elapsed_minutes`` return None, and a FUTURE-dated one makes it negative;
    either way the deadline never trips and the loop is unbounded forever. So a
    stamp that cannot bound the loop is restamped to now, and the caller persists
    that, which bounds the loop from this fire forward.
    """
    now = datetime.now(timezone.utc)
    parsed = parse_iso(value) if isinstance(value, str) else None
    if parsed is None:
        return now.isoformat(), True
    ahead = (parsed - now).total_seconds() / 60.0
    if ahead > CLOCK_SKEW_TOLERANCE_MINUTES:
        return now.isoformat(), True
    return value, False


# Load-status classifiers for the ordered decision tree in persistent-mode.py:
# corrupt-check (unparseable) → schema-check (parseable but newer) → adoption.
LOAD_OK = "ok"
LOAD_MISSING = "missing"
LOAD_CORRUPT = "corrupt"
LOAD_FUTURE_SCHEMA = "future_schema"


def atomic_write_json(path: Path, data: dict) -> None:
    """Atomically write ``data`` as JSON to ``path``, 0600 from birth.

    ``tempfile.mkstemp`` creates the temp file 0600 by
    construction in the TARGET directory (same filesystem → ``os.replace`` is
    atomic; no torn state). Each writer owns a UNIQUE temp path, so concurrent
    writers never clobber one another's temp — last ``os.replace`` wins with a
    fully-formed file. The ``finally`` cleanup removes the temp on failure
    (and is a suppressed no-op on success, where the temp was already renamed
    away). Directory creation lives ONLY here: reads never mkdir.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)  # atomic; tmp no longer exists after this
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)  # no-op on success, cleans up on failure


def read_state_json(path: Path):
    """Read + classify a state file. Returns ``(data | None, status)``.

    Ordered per the decision tree (corrupt precedes schema): missing → default;
    unparseable/non-object → LOAD_CORRUPT; parseable object whose ``schema`` is
    newer than SCHEMA_VERSION → LOAD_FUTURE_SCHEMA (refuse to touch); otherwise
    LOAD_OK with the parsed dict.
    """
    if not path.is_file():
        return None, LOAD_MISSING
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None, LOAD_CORRUPT
    if not isinstance(data, dict):
        return None, LOAD_CORRUPT
    schema = data.get("schema", 1)
    if isinstance(schema, int) and schema > SCHEMA_VERSION:
        return None, LOAD_FUTURE_SCHEMA
    return data, LOAD_OK


# The lock a writer holds for the WHOLE read-modify-write of a state file. It
# lives on a sibling `<state-file>.lock` and never on the state file itself:
# atomic_write_json replaces the state file's inode, so a lock taken on the old
# inode guards nothing once the first writer lands (the next writer opens the NEW
# inode and takes an uncontended lock, and the two run concurrently anyway). The
# sibling's inode is stable, so it serializes every writer.
STATE_LOCK_SUFFIX = ".lock"

# Bounded, never an indefinite block: the Stop hook's budget in hooks.json is 5s,
# so a stuck holder must surface as a fail-open diagnostic rather than a hung hook.
STATE_LOCK_TIMEOUT_SECONDS = 2.0
_STATE_LOCK_POLL_SECONDS = 0.02


class StateLockError(Exception):
    """The state file's lock could not be taken (timed out, or unopenable).

    Callers must treat this as a FAILURE TO WRITE, never as a write: the Stop hook
    allows the stop with a loud diagnostic, the CLI exits nonzero.
    """


def state_lock_path(path: Path) -> Path:
    """The sibling lock file for a state file (see STATE_LOCK_SUFFIX)."""
    return path.with_name(path.name + STATE_LOCK_SUFFIX)


@contextlib.contextmanager
def state_lock(path: Path, timeout: float = STATE_LOCK_TIMEOUT_SECONDS):
    """Hold the exclusive interprocess lock for one read-modify-write of ``path``.

    Without fcntl (non-POSIX) the body still runs, UNLOCKED: degrading to the old
    last-writer-wins behavior beats wedging every state write on a platform that
    cannot lock. It says so on stderr rather than degrading silently.
    """
    if fcntl is None:
        print(
            "[crew] fcntl is unavailable on this platform: state writes are not "
            "serialized, so a concurrent writer can lose an update.",
            file=sys.stderr,
        )
        yield
        return

    lock_file = state_lock_path(path)
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(lock_file), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        raise StateLockError(f"cannot open {lock_file.name}: {exc}") from exc
    try:
        # Keep the lock file young: the session-start sweep deletes crew artifacts
        # past MAX_AGE_DAYS, and deleting a lock that a live loop still uses would
        # split its writers across two inodes (see STATE_LOCK_SUFFIX).
        with contextlib.suppress(OSError):
            os.utime(fd)
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise StateLockError(
                        f"timed out after {timeout:g}s waiting for "
                        f"{lock_file.name}: {exc}"
                    ) from exc
                time.sleep(_STATE_LOCK_POLL_SECONDS)
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def update_state_json(path: Path, mutate, default: Optional[dict] = None):
    """The ONE serialized read-modify-write over a state file. Returns ``(data, status)``.

    EVERY writer goes through this (the Stop hook and the crew-state CLI).
    ``atomic_write_json`` makes the WRITE atomic but says nothing about the
    read-modify-write SEQUENCE around it: two unlocked writers each replace the
    file with a dict built from the state they read BEFORE the other landed, so one
    of the two updates is silently gone. That lost update is how a `set` of an
    allowlisted field could roll back a Stop-hook counter bump, and how a hook save
    could revert an agent-owned field.

    ``mutate(dict) -> dict`` is called with the dict on disk NOW and must touch only
    the keys its caller owns. The read keeps ``read_state_json``'s classification: a
    corrupt or newer-schema file is NOT mutated (returns ``(None, status)``), so each
    caller applies its own refuse-to-touch handling; a missing file starts from
    ``default``. Raises StateLockError when the lock cannot be taken.
    """
    with state_lock(path):
        data, status = read_state_json(path)
        if status in (LOAD_CORRUPT, LOAD_FUTURE_SCHEMA):
            return None, status
        if data is None:
            data = dict(default) if default else {}
        updated = mutate(data)
        atomic_write_json(path, updated)
        return updated, status


def normalize_overrides(value) -> list:
    """The advisory names a `--force` completion recorded over, read defensively.

    `record-verdict` stamps `last_verdict_overrides` only when a forced record
    tripped advisories, so the key is often absent, and the state file is
    hand-editable, so a schema-3 stamp guarantees neither presence nor a list of
    strings. Anything but a list of non-empty strings reads as "no overrides"
    (a clean sign-off) rather than raising into a surface that only describes.
    """
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str) and v]


def override_completion_note(value) -> str:
    """Honest fragment for a completion recorded OVER advisories on a human's
    `--force`, or "" for a clean sign-off (empty/absent overrides).

    Keyed on the `last_verdict_overrides` stamp so every surface that would
    otherwise call a forced completion "approved"/"signed off" says instead that
    it was recorded over one or more advisories on a human's explicit override,
    naming them. Single-sourced so the banner and the nudge never disagree.
    """
    names = normalize_overrides(value)
    if not names:
        return ""
    return (
        f"recorded over {len(names)} advisory condition(s) "
        f"({', '.join(names)}) on a human's explicit --force, not a clean "
        f"panel sign-off"
    )


def coalesce_task(data: dict) -> str:
    """The loop's task text off a state dict, coalescing the legacy keys.

    Schema-2 files carried the text as ``prompt`` (build) or
    ``task_description`` (measure-twice); schema 3 unifies them as ``task``.
    The read-side coalesce keeps a mid-upgrade ACTIVE loop's task visible in
    the nudge, ``show``, and the session-start banner. This is the whole
    compatibility story, not a migration layer.
    """
    for key in ("task", "prompt", "task_description"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


@dataclass
class LoopState:
    """The ONE state shape for both persistence loops (build and measure-twice).

    The two on-disk file prefixes (``build-state-*`` / ``measure-twice-state-*``)
    and the ``bl``/``mt`` aliases stay: they are load-bearing across the Stop
    hook, state discovery, and the session-start cleanup globs. What unified is
    the SHAPE: one dataclass, one ``task`` field, one allowlist, plus the
    review-gate fields the completion machinery reads.
    """
    active: bool = False
    loop: str = ""                 # "bl" | "mt"
    task: str = ""                 # was prompt / task_description
    plan_file: str = ""            # meaningful for mt; inert but accepted on bl
                                   # (one shared allowlist governs both loops)
    phase: str = "drafting"        # drafting | reviewing | done
    revision_round: int = 0
    last_verdict: str = ""         # APPROVED | REVISE | REJECT | FAILED
    # The advisory names a `--force` completion was recorded over (empty on a
    # clean sign-off). record-verdict WRITES this; the model carries it so the
    # banner and nudge can describe a forced completion honestly.
    last_verdict_overrides: list = field(default_factory=list)
    run_id: str = ""
    target_sha256: str = ""        # full 64-hex
    target_spec: str = ""          # bare resolver spec, never "auto"
    target_base: str = ""
    expected_seats: list = field(default_factory=list)
    consecutive_review_failures: int = 0
    exit_kind: str = ""            # approved | cancelled | review_failed | force_exit
    session_id: str = ""
    # Hook-owned fields: the Stop hook is the ONLY writer of all five. There is
    # deliberately no round counter among them, because nothing in the Stop path
    # can honestly observe a revision round (revision_round above is written by
    # the review verbs, never by the hook).
    stop_fires: int = 0
    max_stop_fires: int = DEFAULT_MAX_STOP_FIRES
    started_at: str = ""
    deadline_minutes: int = DEFAULT_DEADLINE_MINUTES
    # Second half of the no-deadline marker: honored only together with
    # deadline_minutes == 0, written only by init (see NO_DEADLINE).
    no_deadline: bool = False
    parked_fires: int = 0
    max_parked_fires: int = DEFAULT_MAX_PARKED_FIRES

    # The ONLY fields `crew state set` will write. Everything the termination
    # predicate reads is refused because a safety limit the agent can rewrite is
    # not a safety limit (a live loop once reset its own counter 30 times), and
    # the verdict/run-identity fields are refused because they belong to the
    # review verbs that VALIDATE what they record; `set` would be an unaudited
    # back door around that gate.
    AGENT_SETTABLE: ClassVar[frozenset] = frozenset({"plan_file"})

    @classmethod
    def load_with_status(cls, path: Path):
        """Load + classify. Returns ``(LoopState, status)``.

        status ∈ {ok, missing, corrupt, future_schema}. Non-OK statuses return
        the DEFAULT state so callers that only want a value degrade safely; the
        status lets the Stop hook distinguish corrupt (diagnose + set aside)
        from a newer-schema file (refuse to touch).
        """
        data, status = read_state_json(path)
        if status != LOAD_OK:
            return cls(), status
        seats = data.get("expected_seats")
        return cls(
            active=is_active_value(data.get("active", False)),
            loop=data.get("loop", ""),
            task=coalesce_task(data),
            plan_file=data.get("plan_file", ""),
            phase=data.get("phase", "drafting"),
            revision_round=data.get("revision_round", 0),
            last_verdict=data.get("last_verdict", ""),
            last_verdict_overrides=normalize_overrides(
                data.get("last_verdict_overrides")),
            run_id=data.get("run_id", ""),
            target_sha256=data.get("target_sha256", ""),
            target_spec=data.get("target_spec", ""),
            target_base=data.get("target_base", ""),
            expected_seats=seats if isinstance(seats, list) else [],
            consecutive_review_failures=data.get("consecutive_review_failures", 0),
            exit_kind=data.get("exit_kind", ""),
            session_id=data.get("session_id", ""),
            stop_fires=data.get("stop_fires", 0),
            max_stop_fires=data.get("max_stop_fires", DEFAULT_MAX_STOP_FIRES),
            started_at=data.get("started_at", ""),
            deadline_minutes=data.get("deadline_minutes", DEFAULT_DEADLINE_MINUTES),
            no_deadline=data.get("no_deadline", False) is True,
            parked_fires=data.get("parked_fires", 0),
            max_parked_fires=data.get("max_parked_fires", DEFAULT_MAX_PARKED_FIRES),
        ), LOAD_OK

    @classmethod
    def load(cls, path: Path) -> "LoopState":
        """Load from file, returning default state if file is missing/unreadable."""
        return cls.load_with_status(path)[0]

    def save(self, path: Path) -> None:
        """Atomically save to file, 0600 from birth, stamping the schema."""
        data = asdict(self)
        data["schema"] = SCHEMA_VERSION
        atomic_write_json(path, data)


@dataclass
class TodoItem:
    """A single todo item."""
    content: str = ""
    status: str = "pending"  # pending, in_progress, completed, cancelled
    active_form: str = ""  # Present tense description

    @property
    def is_incomplete(self) -> bool:
        return self.status not in ("completed", "cancelled")


def load_todos(path: Path) -> list[TodoItem]:
    """Load todo items from a JSON file."""
    if not path.is_file():
        return []
    try:
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, list):
            return [
                TodoItem(
                    content=item.get("content", ""),
                    status=item.get("status", "pending"),
                    active_form=item.get("activeForm", ""),
                )
                for item in data
            ]
    except (OSError, json.JSONDecodeError):
        pass
    return []


def count_incomplete_todos(path: Path) -> int:
    """Count incomplete todos in a file."""
    return sum(1 for todo in load_todos(path) if todo.is_incomplete)


# =============================================================================
# Utility Functions
# =============================================================================

def is_verbose() -> bool:
    """Return True when CREW_VERBOSE is set to a truthy value."""
    return os.environ.get("CREW_VERBOSE", "").lower() in ("1", "true", "yes")


def truncate(text: str, max_length: int = 120) -> str:
    """Truncate text to max_length chars total (including ellipsis).

    If len(text) > max_length, returns text[:max_length-3] + '...' (total = max_length).
    If len(text) <= max_length, returns text unchanged.
    """
    if len(text) <= max_length:
        return text
    return text[:max_length - 3] + "..."


def read_hook_input() -> dict:
    """Read and parse JSON input from stdin."""
    import sys
    try:
        return json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return {}


def get_file_age_days(path: Path) -> int:
    """Get file age in days."""
    import time
    try:
        age_seconds = time.time() - path.stat().st_mtime
        return int(age_seconds / 86400)
    except OSError:
        return 999


