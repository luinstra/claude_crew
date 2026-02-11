#!/usr/bin/env python3
"""
Data models for Claude Crew hooks.

All JSON structures used by hooks are defined here as dataclasses
for type safety, validation, and self-documentation.
"""

from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional, Any
import json
import os


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

    @classmethod
    def from_dict(cls, data: dict) -> "StopInput":
        return cls(
            directory=_get_project_dir(data),
            session_id=data.get("session_id", data.get("sessionId", "")),
        )

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

    def to_json(self) -> str:
        """Serialize to JSON for hook output."""
        result: dict[str, Any] = {"continue": self.continue_}
        if self.message is not None:
            result["message"] = self.message
        if self.reason is not None:
            result["reason"] = self.reason
        return json.dumps(result)

    @classmethod
    def allow(cls, message: Optional[str] = None) -> "HookResult":
        """Create a result that allows the action to continue."""
        return cls(continue_=True, message=message)

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

@dataclass
class FeedbackLoopState:
    """State for feedback loop persistence with advisor verification."""
    active: bool = False
    prompt: str = ""
    iteration: int = 1
    max_iterations: int = 20
    completion_promise: str = "DONE"
    session_id: str = ""

    @classmethod
    def load(cls, path: Path) -> "FeedbackLoopState":
        """Load from file, returning default state if file doesn't exist."""
        if not path.is_file():
            return cls()
        try:
            with open(path) as f:
                data = json.load(f)
            return cls(
                active=data.get("active", False),
                prompt=data.get("prompt", ""),
                iteration=data.get("iteration", 1),
                max_iterations=data.get("max_iterations", 20),
                completion_promise=data.get("completion_promise", "DONE"),
                session_id=data.get("session_id", ""),
            )
        except (OSError, json.JSONDecodeError):
            return cls()

    def save(self, path: Path) -> None:
        """Save to file with restrictive permissions."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(asdict(self), f, indent=2)
        os.chmod(path, 0o600)


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
    iteration: int = 1
    max_iterations: int = 10
    last_verdict: str = ""  # APPROVED, REVISE, REJECT, or empty
    session_id: str = ""

    @classmethod
    def load(cls, path: Path) -> "MeasureTwiceState":
        """Load from file, returning default state if file doesn't exist."""
        if not path.is_file():
            return cls()
        try:
            with open(path) as f:
                data = json.load(f)
            return cls(
                active=data.get("active", False),
                task_description=data.get("task_description", ""),
                plan_file=data.get("plan_file", ""),
                iteration=data.get("iteration", 1),
                max_iterations=data.get("max_iterations", 10),
                last_verdict=data.get("last_verdict", ""),
                session_id=data.get("session_id", ""),
            )
        except (OSError, json.JSONDecodeError):
            return cls()

    def save(self, path: Path) -> None:
        """Save to file with restrictive permissions."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(asdict(self), f, indent=2)
        os.chmod(path, 0o600)
