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
import tempfile


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


def _get_project_dir(data: dict) -> str:
    """Get project directory from hook input, preferring CLAUDE_PROJECT_DIR."""
    # CLAUDE_PROJECT_DIR is the authoritative project root
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if project_dir:
        return project_dir
    # Fall back to input fields, then cwd
    return data.get("directory", data.get("cwd", os.getcwd()))


@dataclass
class SessionStartInput:
    """Input structure for SessionStart hooks."""
    directory: str = ""
    session_id: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "SessionStartInput":
        return cls(
            directory=_get_project_dir(data),
            session_id=data.get("session_id", data.get("sessionId", "")),
        )

    @property
    def directory_path(self) -> Path:
        return Path(self.directory)


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
    # Set by Claude Code when THIS Stop was already continued by a hook. Read for
    # diagnostics only: crew enforces its own bound via BuildState.stop_fires.
    stop_hook_active: bool = False

    @classmethod
    def from_dict(cls, data: dict) -> "StopInput":
        tasks = data.get("background_tasks")
        crons = data.get("session_crons")
        return cls(
            directory=_get_project_dir(data),
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

        Deliberately BROAD (any tracked background shell parks the Stop), which
        also means an unrelated long-lived shell (a dev server, a `tail -f`)
        reads as parked forever. That false positive is bounded on the state
        side by `max_parked_fires`, not narrowed here: the payload carries no
        field that reliably separates crew's own seats from other work.
        """
        return bool(self.background_tasks) or bool(self.session_crons)

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
# The parked counters and the removal of the old round counter did NOT need a new
# version: every read defaults a missing field and ignores an unknown one, so a
# file stays legible in both directions. Bumping would make an older install
# refuse to touch a live loop for a change it degrades safely on.
SCHEMA_VERSION = 2

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
# a stall clock forever.
DEFAULT_DEADLINE_MINUTES = 60

# The policy ceiling on that wall clock. `init` is invoked BY the agent, so the
# deadline it may request is bounded on BOTH ends: a bound the bounded thing can
# widen (or delete with a negative) is not a bound.
MAX_DEADLINE_MINUTES = 240

# Consecutive parked Stops (see StopInput.is_parked) allowed before the hook
# nudges anyway. Generous on purpose: a genuine seat wait costs one or two parked
# fires per round, so honest waiting stays effectively free. Its job is the case
# the park signal cannot distinguish: an unrelated background shell that never
# exits would otherwise park every Stop and silently disable the loop.
DEFAULT_MAX_PARKED_FIRES = 20


def utc_now_iso() -> str:
    """Timezone-aware UTC timestamp for the state files' clock fields."""
    return datetime.now(timezone.utc).isoformat()


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


@dataclass
class BuildState:
    """State for build loop persistence with multi-model panel verification."""
    active: bool = False
    prompt: str = ""
    completion_promise: str = "DONE"
    session_id: str = ""
    # Hook-owned fields: the Stop hook is the ONLY writer of all five. There is
    # deliberately no round counter here, because nothing in the Stop path can
    # honestly observe a revision round.
    stop_fires: int = 0
    max_stop_fires: int = DEFAULT_MAX_STOP_FIRES
    started_at: str = ""
    deadline_minutes: int = DEFAULT_DEADLINE_MINUTES
    parked_fires: int = 0
    max_parked_fires: int = DEFAULT_MAX_PARKED_FIRES

    # The ONLY fields `crew state set` will write. Everything else (every field
    # the termination predicate reads) is refused, because a safety limit the
    # agent can rewrite is not a safety limit: a live loop reset its own counter
    # 30 times to stay under a cap it kept hitting merely for waiting, making the
    # force-exit unreachable.
    AGENT_SETTABLE: ClassVar[frozenset] = frozenset({"completion_promise"})

    @classmethod
    def load_with_status(cls, path: Path):
        """Load + classify. Returns ``(BuildState, status)``.

        status ∈ {ok, missing, corrupt, future_schema}. Non-OK statuses return
        the DEFAULT state so callers that only want a value degrade safely; the
        status lets the Stop hook distinguish corrupt (diagnose + set aside)
        from a newer-schema file (refuse to touch).
        """
        data, status = read_state_json(path)
        if status != LOAD_OK:
            return cls(), status
        return cls(
            active=data.get("active", False),
            prompt=data.get("prompt", ""),
            completion_promise=data.get("completion_promise", "DONE"),
            session_id=data.get("session_id", ""),
            stop_fires=data.get("stop_fires", 0),
            max_stop_fires=data.get("max_stop_fires", DEFAULT_MAX_STOP_FIRES),
            started_at=data.get("started_at", ""),
            deadline_minutes=data.get("deadline_minutes", DEFAULT_DEADLINE_MINUTES),
            parked_fires=data.get("parked_fires", 0),
            max_parked_fires=data.get("max_parked_fires", DEFAULT_MAX_PARKED_FIRES),
        ), LOAD_OK

    @classmethod
    def load(cls, path: Path) -> "BuildState":
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


@dataclass
class MeasureTwiceState:
    """State for measure-twice plan-review-revise loop."""
    active: bool = False
    task_description: str = ""
    plan_file: str = ""  # Path to current plan, e.g., ".crew/plans/auth-system.md"
    last_verdict: str = ""  # APPROVED, REVISE, REJECT, or empty
    session_id: str = ""
    # Hook-owned fields (see BuildState).
    stop_fires: int = 0
    max_stop_fires: int = DEFAULT_MAX_STOP_FIRES
    started_at: str = ""
    deadline_minutes: int = DEFAULT_DEADLINE_MINUTES
    parked_fires: int = 0
    max_parked_fires: int = DEFAULT_MAX_PARKED_FIRES

    # See BuildState.AGENT_SETTABLE. `last_verdict` and `plan_file` are settable
    # because recording a verdict and pointing at the current plan are the
    # orchestrator's job; every field the Stop hook's bounds read is not.
    AGENT_SETTABLE: ClassVar[frozenset] = frozenset({"last_verdict", "plan_file"})

    @classmethod
    def load_with_status(cls, path: Path):
        """Load + classify. Returns ``(MeasureTwiceState, status)``.

        status ∈ {ok, missing, corrupt, future_schema}; non-OK statuses return
        the DEFAULT state (see BuildState.load_with_status).
        """
        data, status = read_state_json(path)
        if status != LOAD_OK:
            return cls(), status
        return cls(
            active=data.get("active", False),
            task_description=data.get("task_description", ""),
            plan_file=data.get("plan_file", ""),
            last_verdict=data.get("last_verdict", ""),
            session_id=data.get("session_id", ""),
            stop_fires=data.get("stop_fires", 0),
            max_stop_fires=data.get("max_stop_fires", DEFAULT_MAX_STOP_FIRES),
            started_at=data.get("started_at", ""),
            deadline_minutes=data.get("deadline_minutes", DEFAULT_DEADLINE_MINUTES),
            parked_fires=data.get("parked_fires", 0),
            max_parked_fires=data.get("max_parked_fires", DEFAULT_MAX_PARKED_FIRES),
        ), LOAD_OK

    @classmethod
    def load(cls, path: Path) -> "MeasureTwiceState":
        """Load from file, returning default state if file is missing/unreadable."""
        return cls.load_with_status(path)[0]

    def save(self, path: Path) -> None:
        """Atomically save to file, 0600 from birth, stamping the schema."""
        data = asdict(self)
        data["schema"] = SCHEMA_VERSION
        atomic_write_json(path, data)
