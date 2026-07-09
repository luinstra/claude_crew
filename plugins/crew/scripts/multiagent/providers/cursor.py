"""CursorProvider — a LIVE subprocess seat using the Cursor Agent CLI.

One Cursor subscription exposes many models (`agent models`); we run several as
distinct panel seats for cross-model diversity at one price. Each seat is a
``CursorProvider`` instance pinned to a model string. Adding a model is a
ONE-LINE change to ``CURSOR_SEATS`` below — the registry, the engine's
subprocess-seat allowlist (which is registry-derived), and `--seats` all pick it
up automatically.

Adapted to OUR executor `Provider` ABC (`is_available()` + `run()`), NOT the
Enterprise fork's prompt-builder `BaseProvider` — prompt-building stays in
`prompts.py` (the single builder); this seat only EXECUTES, like codex.

Invokes the ``agent`` binary (Cursor's headless agent). ``agent`` is a generic
binary name, so availability is confirmed by an identity probe on
``agent --version`` (see ``is_available`` — note the version is a bare date-stamp
like ``2026.06.24-...`` with NO literal "cursor", so the date-stamp regex is the
load-bearing check).

CLI signature (read-only review default):
    agent --print --mode plan --sandbox enabled --trust --workspace <cwd> --model <model> <prompt>
``--mode plan`` is the read-only enforcement (see ``run``): it applies no edits,
which plain ``--print`` does NOT guarantee. Dropped only for workspace-write.
"""

from __future__ import annotations

import os
import re
import shutil
import time

from multiagent import config
from multiagent.providers import Provider, ProviderResult
from multiagent.providers._proc import TIMEOUT, run_reaped

# ANSI escape code pattern for stripping terminal colour sequences.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mGKHF]")

# Conservative ARG_MAX cap (same as agy): prompt must fit in 256 KB.
_ARG_MAX_BYTES = 256 * 1024

# Cursor's headless `agent --version` prints a bare build date-stamp
# (e.g. "2026.06.24-00-45-58-9f61de7") — NO literal "cursor" — so this regex,
# not a "cursor" substring, is what positively identifies the binary.
_VERSION_DATESTAMP_RE = re.compile(r"^\d{4}\.\d{2}\.\d{2}-")

# === The cursor panel seats — SINGLE SOURCE OF TRUTH ==========================
# name -> model string `agent --model` accepts.
# Add a model = add ONE line here. Model strings verified via `agent models`.
# Retune a seat without code via [seats.<name>].model in .crew/config.toml or
# the global ~/.crew-config.toml.
CURSOR_SEATS: dict[str, str] = {
    "cursor-gpt":      "gpt-5.5-extra-high",
    "cursor-gemini":   "gemini-3.1-pro",
    "cursor-glm":      "glm-5.2-max",
    "cursor-grok":     "grok-4.5-xhigh",
    "cursor-auto":     "auto",
    "cursor-composer": "composer-2.5",
}

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
# Bracketed tags / distinctive headers only — NOT bare "approved"/"revise"
# (an auth banner saying "not approved" would mask itself = false-negative).
_REVIEW_STRUCTURE_MARKERS = (
    # "confidence:" (with the colon) matches the rubric's "CONFIDENCE: <level>"
    # but NOT an auth banner's "confidence level: LOW — please authenticate".
    "[blocking]", "[minor]", "confidence:",
    "direct take", "strongest objection", "risks / tradeoff",
)


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _looks_like_review(low: str) -> bool:
    return (
        len(low.strip()) > _AUTH_BANNER_MAX_CHARS
        or any(tok in low for tok in _REVIEW_STRUCTURE_MARKERS)
    )


def _auth_failure_marker(text: str) -> str | None:
    """The matched auth marker if output is a real auth banner, else None.

    Lowercases internally (no hidden 'caller must pre-lowercase' contract), so
    it's correct whether handed raw output or an already-lowered string.
    """
    low = text.lower()
    if _looks_like_review(low):
        return None
    for marker in _AUTH_MARKERS:
        if marker in low:
            return marker
    return None


class CursorProvider(Provider):
    """A review-panel seat backed by the Cursor Agent CLI subprocess.

    Pinned to one model. Multiple named instances (see ``CURSOR_SEATS``) run as
    distinct seats — ``cursor-auto`` and ``cursor-composer`` are the default
    cursor model-seats; ``cursor-gpt``, ``cursor-gemini``, ``cursor-glm``, and
    ``cursor-grok`` are registered but opt-in (via ``--seats`` / ``--panel
    cursor``).
    """

    # EXPLICIT opt-in (fail-CLOSED ABC default is False): cursor honors
    # workspace-write by DROPPING --mode plan so edits apply (--sandbox enabled
    # still blocks network + out-of-workspace). A valid /crew:dispatch write seat.
    # cursor already pins its own cwd + --workspace to CLAUDE_WORKING_DIRECTORY,
    # so it needs no separate workspace-write cwd pin.
    supports_workspace_write = True

    def __init__(
        self,
        name: str = "cursor",
        default_model: str | None = None,
    ) -> None:
        self.name = name
        self._default_model = default_model

    def is_available(self) -> tuple[bool, str]:
        """PATH check + a Cursor-identity probe on ``agent --version``.

        ``agent`` is a generic binary name, so a PATH hit alone isn't enough.
        Cursor's headless ``--version`` prints a bare build date-stamp (e.g.
        ``2026.06.24-...``) with NO literal "cursor", so we accept the binary
        when the version EITHER matches that date-stamp shape OR contains
        "cursor" (older builds). Unlike codex/agy's pure-PATH check this spawns a
        short probe — acceptable: a wrong skip is never-choke-safe (the seat just
        sits out), whereas a wrong run would feed a non-Cursor tool a prompt.
        """
        if shutil.which("agent") is None:
            return False, "agent not found on PATH"
        try:
            probe = run_reaped(["agent", "--version"], timeout=10)
        except OSError:
            return False, "agent binary found but does not appear to be Cursor Agent"
        if probe is TIMEOUT:
            return False, "agent binary found but does not appear to be Cursor Agent"
        _rc, probe_out, probe_err = probe
        combined = ((probe_out or "") + (probe_err or "")).strip()
        first_line = combined.splitlines()[0] if combined else ""
        if not (_VERSION_DATESTAMP_RE.match(first_line) or "cursor" in combined.lower()):
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

        ``sandbox="read-only"`` (the default, and what review/debate/council seats
        use) runs the agent in ``--mode plan`` — analyse + read-only shell (it CAN
        run ``git diff`` and read files, and it produces its review text), but it
        does NOT apply edits. This is load-bearing and EMPIRICALLY verified: plain
        ``--print --sandbox enabled`` (no ``--mode``) actually WRITES to the
        workspace despite the headless docs saying changes are "only proposed"
        without ``--force`` — a review seat must never mutate the repo. ``--sandbox
        enabled`` additionally blocks network + out-of-workspace access. Only
        ``sandbox="workspace-write"`` drops ``--mode plan`` (writes permitted).
        """
        # Precedence: explicit model (CLI --model) > per-repo .crew/config.toml >
        # global ~/.crew-config.toml ([seats.<name>].model, both via
        # config.seat_model) > the seat's built-in default.
        chosen_model = (
            model
            or config.seat_model(self.name)
            or self._default_model
        )
        if not chosen_model:
            return ProviderResult(
                name=self.name, model=None, ok=False, output="",
                error=(f"cursor seat {self.name!r} has no model configured; pass "
                       f"--model or set [seats.{self.name}].model in .crew/config.toml "
                       f"or ~/.crew-config.toml"),
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

        cmd = ["agent", "--print"]
        # read-only review posture: --mode plan applies NO edits (verified) while
        # still allowing read-only shell (git diff) + producing review output.
        if sandbox != "workspace-write":
            cmd += ["--mode", "plan"]
        cmd += [
            "--sandbox", "enabled", "--trust",
            "--workspace", cwd, "--model", chosen_model, prompt,
        ]
        start = time.monotonic()
        # Shared reaped runner: start_new_session + SIGTERM→SIGKILL killpg
        # teardown on timeout so a hung cursor agent can't orphan billable
        # grandchildren. OSError (launch failure) preserved as before.
        try:
            result = run_reaped(cmd, timeout=timeout, cwd=cwd)
        except OSError as exc:
            return ProviderResult(
                name=self.name, model=chosen_model, ok=False, output="",
                error=f"Failed to launch agent: {exc}",
                elapsed=time.monotonic() - start,
            )
        if result is TIMEOUT:
            return ProviderResult(
                name=self.name, model=chosen_model, ok=False, output="",
                error=f"Cursor Agent timed out after {timeout}s",
                elapsed=time.monotonic() - start,
            )
        returncode, proc_stdout, proc_stderr = result

        elapsed = time.monotonic() - start
        raw_output = _strip_ansi(proc_stdout)
        combined_lower = (raw_output + proc_stderr).lower()
        auth_marker = _auth_failure_marker(combined_lower)
        if auth_marker is not None:
            return ProviderResult(
                name=self.name, model=chosen_model, ok=False, output=raw_output,
                error=(f"Cursor Agent authentication required (detected: {auth_marker!r}). "
                       "Sign in at cursor.com/login."),
                elapsed=elapsed,
            )
        if returncode != 0:
            return ProviderResult(
                name=self.name, model=chosen_model, ok=False, output=raw_output,
                error=(f"agent exited with code {returncode}. "
                       f"stderr: {_strip_ansi(proc_stderr)[:500]}"),
                elapsed=elapsed,
            )
        # Empty output at exit 0 is NOT a valid review (mirrors codex/agy): an
        # all-empty panel must not be rendered as an OK "(no output)" review.
        if not raw_output.strip():
            stderr_clean = _strip_ansi(proc_stderr).strip()
            return ProviderResult(
                name=self.name, model=chosen_model, ok=False, output="",
                error=("agent returned empty output at exit 0"
                       + (f"; stderr: {stderr_clean[:500]}" if stderr_clean else "")),
                elapsed=elapsed,
            )
        return ProviderResult(
            name=self.name, model=chosen_model, ok=True, output=raw_output,
            error=None, elapsed=elapsed,
        )


# =============================================================================
# To add another Cursor model as a panel seat:
#   1. Add ONE line to CURSOR_SEATS above: "cursor-<x>": "<model>".
#      (Find the model string with `agent models`.)
#   2. That's it. providers/__init__._build_registry() registers every
#      CURSOR_SEATS entry, the engine's subprocess-seat allowlist is derived from
#      the registry, and `--seats cursor-<x>` works. Optionally add it to a
#      command's DEFAULT panel vocabulary (commands/*.md) if it should run by default.
# =============================================================================
