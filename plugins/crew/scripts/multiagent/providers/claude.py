"""ClaudeProvider -- drives the Claude CLI as a read-only subprocess seat.

The adapter intentionally uses ``--permission-mode plan`` as its sole
read-only boundary. Prompt text is delivered through stdin and the CLI's
default text output is normalized into the shared ProviderResult contract.
"""

from __future__ import annotations

import os
import re
import shutil
import time

from multiagent import channels
from state_discovery import crew_base

from . import Provider, ProviderContinuation, ProviderResult
from ._proc import TIMEOUT, run_reaped


# Scrub harness and session identity names, plus the crew override, while
# preserving authentication and PATH inputs. Exclude CLAUDE_PLUGIN_ROOT and
# CODEX_COMPANION_* names because they are not identity markers for this child.
# Re-derive this list when the host-owned marker contract changes; the exact
# non-Claude marker tables are unioned at call time below.
_SCRUBBED_ENV_NAMES = (
    "AI_AGENT",
    "CLAUDECODE",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_EFFORT",
    "CLAUDE_PID",
    "CLAUDE_PLUGIN_DATA",
    "CREW_HOST",
)

_ALLOWED_TOOLS = (
    "Read,Grep,Glob,Bash(git diff:*),Bash(git --no-pager diff:*),"
    "Bash(git show:*),Bash(git log:*),Bash(git status:*),Bash(git ls-files:*)"
)
# Module-local ANSI stripping pattern for provider output.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mGKHF]")


def strip_ansi(text: str) -> str:
    """Remove terminal color/control sequences from CLI text."""
    return _ANSI_RE.sub("", text)


def _child_env() -> dict[str, str]:
    """Build a child environment without inherited harness identity markers."""
    env = dict(os.environ)
    scrubbed = (
        set(_SCRUBBED_ENV_NAMES)
        | set(channels.codex_host_markers())
        | set(channels.cursor_host_markers())
    )
    for name in scrubbed:
        env.pop(name, None)
    return env


class ClaudeProvider(Provider):
    """A read-only review seat backed by the ``claude`` CLI."""

    # Read-only by design; write support is a deliberate non-goal here.
    supports_workspace_write = False
    # The CLI exposes resume flags, but continuation is deliberately not driven
    # by this adapter.
    supports_continuation = False
    DISPATCH_OPTIONS: dict = {}

    def __init__(self, name: str = "claude", default_model: str | None = None) -> None:
        self.name = name
        self._default_model = default_model

    def is_available(self) -> tuple[bool, str]:
        """Check installation only; authentication is a run-time outcome."""
        if shutil.which("claude") is not None:
            return True, ""
        return False, "claude not found on PATH"

    def run(
        self,
        prompt: str,
        *,
        sandbox: str = "read-only",
        model: str | None = None,
        timeout: int = 300,
        dispatch_options: dict | None = None,
        continuation: ProviderContinuation | None = None,
    ) -> ProviderResult:
        """Run one read-only Claude turn and normalize its result."""
        del dispatch_options, continuation
        start = time.monotonic()
        chosen_model = model or self._default_model

        if sandbox == "workspace-write":
            return ProviderResult(
                name=self.name,
                model=chosen_model,
                ok=False,
                output="",
                error="claude seat does not support workspace-write in this build",
                elapsed=time.monotonic() - start,
            )

        argv = [
            "claude",
            "-p",
            "--permission-mode",
            "plan",
            "--allowedTools",
            _ALLOWED_TOOLS,
        ]
        if chosen_model:
            argv += ["--model", chosen_model]

        try:
            result = run_reaped(
                argv,
                input_text=prompt,
                timeout=timeout,
                cwd=str(crew_base()),
                env=_child_env(),
            )
        except OSError as exc:
            return ProviderResult(
                name=self.name,
                model=chosen_model,
                ok=False,
                output="",
                error=f"claude launch failed: {exc}",
                elapsed=time.monotonic() - start,
            )

        if result is TIMEOUT:
            return ProviderResult(
                name=self.name,
                model=chosen_model,
                ok=False,
                output="",
                error=f"claude timed out after {timeout}s",
                elapsed=time.monotonic() - start,
            )

        returncode, stdout, stderr = result
        output = strip_ansi(stdout or "").strip()
        if returncode != 0:
            error = strip_ansi(stderr or "").strip()
            return ProviderResult(
                name=self.name,
                model=chosen_model,
                ok=False,
                output="",
                error=error or f"claude exited with status {returncode}",
                elapsed=time.monotonic() - start,
            )
        if not output:
            return ProviderResult(
                name=self.name,
                model=chosen_model,
                ok=False,
                output="",
                error="claude returned no output",
                elapsed=time.monotonic() - start,
            )
        return ProviderResult(
            name=self.name,
            model=chosen_model,
            ok=True,
            output=output,
            error=None,
            elapsed=time.monotonic() - start,
        )
