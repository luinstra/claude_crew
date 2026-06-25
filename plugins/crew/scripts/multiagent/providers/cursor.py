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

# Cursor's headless `agent --version` prints a bare build date-stamp
# (e.g. "2026.06.24-00-45-58-9f61de7") — NO literal "cursor" — so this regex,
# not a "cursor" substring, is what positively identifies the binary.
_VERSION_DATESTAMP_RE = re.compile(r"^\d{4}\.\d{2}\.\d{2}-")

# === The cursor panel seats — SINGLE SOURCE OF TRUTH ==========================
# name -> (model string `agent --model` accepts, env var to override it).
# Add a model = add ONE line here. Model strings verified via `agent models`.
# Env override lets you retune a seat without code (e.g. CREW_MA_GLM_MODEL=...).
CURSOR_SEATS: dict[str, tuple[str, str]] = {
    "cursor-gemini":   ("gemini-3.1-pro", "CREW_MA_GEMINI_MODEL"),
    "cursor-glm":      ("glm-5.2-max",    "CREW_MA_GLM_MODEL"),
    "cursor-composer": ("composer-2.5",   "CREW_MA_COMPOSER_MODEL"),
}

# Fallback env var for a bare/unconfigured CursorProvider instance.
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
    distinct seats — ``cursor-gemini``, ``cursor-glm``, ``cursor-composer``, …
    """

    def __init__(
        self,
        name: str = "cursor",
        default_model: str | None = None,
        model_env_var: str = _MODEL_ENV_VAR,
    ) -> None:
        self.name = name
        self._default_model = default_model
        self._model_env_var = model_env_var

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
            result = subprocess.run(
                ["agent", "--version"], capture_output=True, text=True, timeout=10,
            )
            combined = (result.stdout + result.stderr).strip()
            first_line = combined.splitlines()[0] if combined else ""
            if not (_VERSION_DATESTAMP_RE.match(first_line) or "cursor" in combined.lower()):
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
        chosen_model = model or os.environ.get(self._model_env_var, self._default_model)
        if not chosen_model:
            return ProviderResult(
                name=self.name, model=None, ok=False, output="",
                error=(f"cursor seat {self.name!r} has no model configured; set "
                       f"{self._model_env_var} or pass --model"),
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
# To add another Cursor model as a panel seat:
#   1. Add ONE line to CURSOR_SEATS above: "cursor-<x>": ("<model>", "CREW_MA_<X>_MODEL").
#      (Find the model string with `agent models`.)
#   2. That's it. providers/__init__._build_registry() registers every
#      CURSOR_SEATS entry, the engine's subprocess-seat allowlist is derived from
#      the registry, and `--seats cursor-<x>` works. Optionally add it to a
#      command's DEFAULT panel vocabulary (commands/*.md) if it should run by default.
# =============================================================================
