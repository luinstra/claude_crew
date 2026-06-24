"""CursorProvider — a DORMANT subprocess seat using the Cursor Agent CLI.

⚠️  DORMANT / NOT WIRED IN. This file is carried for parity with the Enterprise
    fork (where Cursor is the multi-model provider) but is **inactive here**:
      * it is NOT in the registry (`providers/__init__._build_registry`),
      * it is in NO panel (`--panel`/`--seats` only resolve codex/agy/opus/sonnet),
      * its model strings are commented out (see `_DEFAULT_MODEL` below).
    codex + agy are the live subprocess seats here. Cursor is not installed/used
    on this machine (yet). See the "To activate" note at the bottom.

It is adapted to OUR executor `Provider` ABC (`is_available()` + `run()`), NOT the
fork's prompt-builder `BaseProvider` — so prompt-building stays in `prompts.py`
(the single builder) and this seat only EXECUTES, like codex/agy. If reactivated
it slots straight into the registry with no further surgery.

Invokes the ``agent`` binary (Cursor's headless agent). ``agent`` is a generic
name, so availability is confirmed by checking ``agent --version`` contains
"cursor".

CLI signature:
    agent --print --sandbox enabled --trust --workspace <cwd> --model <model> <prompt>
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time

from multiagent.providers import Provider, ProviderResult

# ANSI escape code pattern for stripping terminal colour sequences.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mGKHF]")

# Conservative ARG_MAX cap (same as agy): prompt must fit in 256 KB.
_ARG_MAX_BYTES = 256 * 1024

# --- Models intentionally COMMENTED OUT (dormant) ----------------------------
# When this provider is reactivated, pick a concrete model here (and register
# named instances in cli.py / the registry, e.g.):
#   _DEFAULT_MODEL = "claude-sonnet-4-6"
#   PROVIDERS["cursor-gpt"]    = CursorProvider("cursor-gpt",    "gpt-5.5-high")
#   PROVIDERS["cursor-gemini"] = CursorProvider("cursor-gemini", "gemini-3.1-pro")
# Left as None so an accidental instantiation fails loudly instead of silently
# invoking some default model.
_DEFAULT_MODEL = None  # type: ignore[assignment]
_MODEL_ENV_VAR = "CREW_MA_CURSOR_MODEL"

# Auth-failure substrings for Cursor-specific error detection.
_AUTH_MARKERS = (
    "sign in", "log in", "authenticate", "not logged in",
    "cursor.com/login", "please login",
)

# Mirror of agy's banner-shape gate (see agy.py detect_auth_or_error): an auth
# banner REPLACES the review — short + structureless — whereas a review OF auth
# code quotes these markers but is long and/or structured. Only treat
# marker-bearing output as an auth failure when it does NOT look like a review,
# so reviewing auth code doesn't false-positive.
_AUTH_BANNER_MAX_CHARS = 1000
_REVIEW_STRUCTURE_MARKERS = (
    "[blocking]", "[minor]", "approved", "revise", "confidence",
    "direct take", "strongest objection", "risks / tradeoff",
)


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _looks_like_review(low: str) -> bool:
    return (
        len(low.strip()) > _AUTH_BANNER_MAX_CHARS
        or any(tok in low for tok in _REVIEW_STRUCTURE_MARKERS)
    )


def _auth_failure_marker(combined_lower: str) -> str | None:
    """The matched auth marker if output is a real auth banner, else None."""
    if _looks_like_review(combined_lower):
        return None
    for marker in _AUTH_MARKERS:
        if marker in combined_lower:
            return marker
    return None


class CursorProvider(Provider):
    """A review-panel seat backed by the Cursor Agent CLI subprocess (DORMANT)."""

    def __init__(self, name: str = "cursor", default_model: str | None = _DEFAULT_MODEL) -> None:
        self.name = name
        self._default_model = default_model

    def is_available(self) -> tuple[bool, str]:
        """PATH check + a Cursor-identity probe on ``agent --version``.

        (NB: unlike codex/agy's pure PATH check, this spawns ``agent --version``
        to disambiguate the generic ``agent`` binary name. Harmless while
        dormant — this provider is never resolved by the registry.)
        """
        if shutil.which("agent") is None:
            return False, "agent not found on PATH"
        try:
            result = subprocess.run(
                ["agent", "--version"], capture_output=True, text=True, timeout=10,
            )
            if "cursor" not in (result.stdout + result.stderr).lower():
                return False, "agent binary found but does not appear to be Cursor Agent"
        except (subprocess.TimeoutExpired, OSError):
            return False, "agent binary found but does not appear to be Cursor Agent"
        return True, ""

    def run(
        self,
        prompt: str,
        *,
        sandbox: str = "read-only",
        model: str | None = None,
        timeout: int = 300,
    ) -> ProviderResult:
        """Invoke Cursor Agent and return a normalized ProviderResult.

        ``sandbox`` is accepted for ABC compatibility; Cursor always runs under
        its own ``--sandbox enabled`` confinement (any value maps to that).
        """
        chosen_model = model or os.environ.get(_MODEL_ENV_VAR, self._default_model)
        if not chosen_model:
            return ProviderResult(
                name=self.name, model=None, ok=False, output="",
                error=("cursor seat has no model configured (dormant); set "
                       f"{_MODEL_ENV_VAR} or pass --model to activate it"),
                elapsed=0.0,
            )
        cwd = os.environ.get("CLAUDE_WORKING_DIRECTORY", os.getcwd())

        prompt_bytes = len(prompt.encode("utf-8"))
        if prompt_bytes > _ARG_MAX_BYTES:
            return ProviderResult(
                name=self.name, model=chosen_model, ok=False, output="",
                error=(f"Prompt too large for subprocess seat ({prompt_bytes} bytes "
                       f"> {_ARG_MAX_BYTES} byte cap). Use a shorter prompt."),
                elapsed=0.0,
            )

        cmd = [
            "agent", "--print", "--sandbox", "enabled", "--trust",
            "--workspace", cwd, "--model", chosen_model, prompt,
        ]
        start = time.monotonic()
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd,
            )
        except subprocess.TimeoutExpired:
            return ProviderResult(
                name=self.name, model=chosen_model, ok=False, output="",
                error=f"Cursor Agent timed out after {timeout}s",
                elapsed=time.monotonic() - start,
            )
        except OSError as exc:
            return ProviderResult(
                name=self.name, model=chosen_model, ok=False, output="",
                error=f"Failed to launch agent: {exc}",
                elapsed=time.monotonic() - start,
            )

        elapsed = time.monotonic() - start
        raw_output = _strip_ansi(proc.stdout)
        combined_lower = (raw_output + proc.stderr).lower()
        auth_marker = _auth_failure_marker(combined_lower)
        if auth_marker is not None:
            return ProviderResult(
                name=self.name, model=chosen_model, ok=False, output=raw_output,
                error=(f"Cursor Agent authentication required (detected: {auth_marker!r}). "
                       "Sign in at cursor.com/login."),
                elapsed=elapsed,
            )
        if proc.returncode != 0:
            return ProviderResult(
                name=self.name, model=chosen_model, ok=False, output=raw_output,
                error=(f"agent exited with code {proc.returncode}. "
                       f"stderr: {_strip_ansi(proc.stderr)[:500]}"),
                elapsed=elapsed,
            )
        return ProviderResult(
            name=self.name, model=chosen_model, ok=True, output=raw_output,
            error=None, elapsed=elapsed,
        )


# =============================================================================
# To activate Cursor as a live seat (currently DORMANT):
#   1. Pick model(s): set _DEFAULT_MODEL above, or register named instances.
#   2. Register it in providers/__init__._build_registry(), e.g.:
#         from .cursor import CursorProvider
#         return {"codex": CodexProvider, "agy": AgyProvider,
#                 "cursor": CursorProvider}
#   3. Add it to the panel vocabulary in cli.py (_resolve_seats / defaults) and
#      the commands' --panel/--seats lists.
#   4. Add tests mirroring test_codex / test_agy_run.
# Until then it is inert: importing this file registers nothing and runs nothing.
# =============================================================================
