"""CursorProvider — a LIVE subprocess seat using the Cursor Agent CLI.

One Cursor subscription exposes many models (`agent models`); we run several as
distinct panel seats for cross-model diversity at one price. Each seat is a
``CursorProvider`` instance pinned to a model string. The seats themselves live
in the catalog (``multiagent/seats.toml``, or a user's own config), and the
registry builds one instance per catalog row: the engine's subprocess-seat
allowlist is registry-derived, so a new cursor row is usable everywhere at once.

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

import json
import os
import re
import shutil
import time
from dataclasses import dataclass

from multiagent.continuation_ids import valid_conversation_id
from state_discovery import crew_base  # the ONE `.crew`/project-root resolver

from multiagent.providers import (
    ContinuationOutcome,
    Provider,
    ProviderContinuation,
    ProviderResult,
)
from multiagent.providers._proc import TIMEOUT, run_reaped

# ANSI escape code pattern for stripping terminal colour sequences.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mGKHF]")

# Conservative ARG_MAX cap (same as agy): prompt must fit in 256 KB.
_ARG_MAX_BYTES = 256 * 1024

# Cursor's headless `agent --version` prints a bare build date-stamp
# (e.g. "2026.06.24-00-45-58-9f61de7") — NO literal "cursor" — so this regex,
# not a "cursor" substring, is what positively identifies the binary.
_VERSION_DATESTAMP_RE = re.compile(r"^\d{4}\.\d{2}\.\d{2}-")

# Per-process continuation capability memo, keyed by the resolved agent binary.
_CONTINUATION_PROBE_CACHE: dict[str, bool] = {}
PROBE_TIMEOUT = 10


@dataclass(frozen=True)
class _CursorStream:
    session_id: str | None
    mismatched_id: str | None
    ids_mismatched: bool
    terminal_seen: bool
    terminal_success: bool
    terminal_result: str
    assistant_deltas: str


_valid_resume_id = valid_conversation_id


def _reset_probe_cache_for_tests() -> None:
    # Test-only reset for isolation of the module-level probe cache.
    _CONTINUATION_PROBE_CACHE.clear()


def _text_from_value(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("text", "delta", "content"):
            text = _text_from_value(value.get(key))
            if text:
                return text
    if isinstance(value, list):
        return "".join(_text_from_value(item) for item in value)
    return ""


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

    Pinned to one model. Multiple named instances run as distinct seats; the
    roster (and each seat's opt-in flag) lives in the catalog, and the seat's name
    and model arrive from its ``SeatSpec`` through the registry factory.
    """

    # EXPLICIT opt-in (fail-CLOSED ABC default is False): cursor honors
    # workspace-write by DROPPING --mode plan so edits apply (--sandbox enabled
    # still blocks network + out-of-workspace). A valid /crew:dispatch write seat.
    # A WORK seat pins its cwd + --workspace to CLAUDE_WORKING_DIRECTORY (the edit
    # tree); a READ-ONLY review seat pins to crew_base() (the project root the run
    # dir + snapshot anchor to) so a divergent process cwd cannot review the wrong
    # repo (or lose sandbox access to the snapshot).
    supports_workspace_write = True
    # Opt-in: this adapter has an exact-ID resume path. This is a coarse gate
    # only, the runtime probe is the per-run authority and downgrades binaries
    # without the required resume surface to a truthful fresh run.
    supports_continuation = True

    # Write-mode dispatch tuning, declared on THIS class (never inherited from
    # the ABC's shared default). Single source: the config validator, the
    # `crew dispatch --options` listing, and the scaffold-config comments all
    # read this dict. Value shape: (expected_type, help); str/bool only.
    DISPATCH_OPTIONS = {
        "force": (bool, "pass --force: allow commands unless explicitly denied "
                        "(headless-write approval)"),
        "approve_mcps": (bool, "pass --approve-mcps: auto-approve every "
                               "configured MCP server"),
    }

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

    def _supports_continuation_runtime(self) -> bool:
        path = shutil.which("agent")
        if not path:
            return False
        cached = _CONTINUATION_PROBE_CACHE.get(path)
        if cached is not None:
            return cached
        supported = self._probe_continuation(path)
        _CONTINUATION_PROBE_CACHE[path] = supported
        return supported

    @staticmethod
    def _probe_continuation(path: str) -> bool:
        try:
            probe = run_reaped([path, "--help"], timeout=PROBE_TIMEOUT)
            if probe is TIMEOUT:
                return False
            returncode, stdout, stderr = probe
            help_text = (stdout or "") + (stderr or "")
            return (
                returncode == 0
                and "--resume" in help_text
                and "--output-format" in help_text
                and "stream-json" in help_text
            )
        except OSError:
            return False

    @staticmethod
    def _parse_stream(stdout: str) -> _CursorStream:
        session_id: str | None = None
        mismatched_id: str | None = None
        ids_mismatched = False
        initialized = False
        terminal_seen = False
        terminal_success = False
        terminal_result = ""
        assistant_deltas: list[str] = []

        for line in (stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except (TypeError, ValueError):
                continue
            if not isinstance(event, dict):
                continue

            event_type = event.get("type")
            is_init = event_type == "system" and event.get("subtype") == "init"
            is_assistant = event_type in {"assistant", "assistant_delta", "assistant.delta"}
            is_result = event_type == "result"
            event_id = event.get("session_id")
            if session_id is not None and "session_id" in event and event_id != session_id:
                ids_mismatched = True
                mismatched_id = event_id if _valid_resume_id(event_id) else None

            if is_init:
                if not initialized:
                    initialized = True
                    if _valid_resume_id(event_id):
                        session_id = event_id

            if not (is_init or is_assistant or is_result):
                continue

            if is_assistant:
                delta = _text_from_value(event.get("delta"))
                if not delta:
                    delta = _text_from_value(event.get("text"))
                if not delta:
                    delta = _text_from_value(event.get("message"))
                if not delta:
                    delta = _text_from_value(event.get("content"))
                if delta:
                    assistant_deltas.append(delta)

            if is_result:
                terminal_seen = True
                subtype = event.get("subtype")
                terminal_success = (
                    subtype == "success"
                    and event.get("is_error") is False
                )
                if terminal_success:
                    terminal_result = _text_from_value(event.get("result"))

        return _CursorStream(
            session_id=session_id,
            mismatched_id=mismatched_id,
            ids_mismatched=ids_mismatched,
            terminal_seen=terminal_seen,
            terminal_success=terminal_success,
            terminal_result=terminal_result,
            assistant_deltas="".join(assistant_deltas),
        )

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
        start = time.monotonic()

        # Precedence: explicit model (CLI --model) > the seat's resolved model
        # (the catalog already folded the config layers over the shipped pin).
        chosen_model = model or self._default_model
        want_continuation = continuation is not None and sandbox == "workspace-write"
        requested_resume_id = continuation.conversation_id if want_continuation else None

        capable = False
        phase = "fresh"
        capability = "unsupported"

        # Every chained-path early exit must thread an outcome through this helper.
        def make_continuation_result(
            *,
            ok: bool,
            output: str,
            error: str | None,
            failure: str = "none",
            conversation_id: str | None = None,
            continuation_id: str | None = None,
        ) -> ProviderResult:
            values = {
                "name": self.name,
                "model": chosen_model,
                "ok": ok,
                "output": output,
                "error": error,
                "elapsed": time.monotonic() - start,
            }
            if want_continuation:
                values["continuation"] = ContinuationOutcome(
                    capability=capability,
                    phase=phase,
                    conversation_id=conversation_id,
                    failure=failure,
                )
                values["continuation_id"] = continuation_id
            return ProviderResult(**values)

        if not chosen_model:
            capable = want_continuation and self._supports_continuation_runtime()
            resume_id = requested_resume_id if capable else None
            phase = "resume" if resume_id is not None else "fresh"
            capability = "supported" if capable else "unsupported"
            return make_continuation_result(
                ok=False,
                output="",
                error=(f"cursor seat {self.name!r} has no model configured; pass "
                       f"--model or set [seats.{self.name}].model in .crew/config.toml "
                       f"or ~/.crew-config.toml"),
                failure="error",
            )

        if requested_resume_id is not None and not _valid_resume_id(requested_resume_id):
            return ProviderResult(
                name=self.name,
                model=chosen_model,
                ok=False,
                output="",
                error="cursor resume ID is invalid",
                elapsed=0.0,
                continuation=ContinuationOutcome(
                    capability="supported",
                    phase="resume",
                    conversation_id=None,
                    failure="error",
                ),
                continuation_id=None,
            )
        capable = want_continuation and self._supports_continuation_runtime()
        resume_id = requested_resume_id if capable else None
        phase = "resume" if resume_id is not None else "fresh"
        capability = "supported" if capable else "unsupported"

        # A WORK seat edits CLAUDE_WORKING_DIRECTORY (the guard's tree); a
        # read-only review seat pins cwd + --workspace to crew_base() so a
        # divergent process cwd reviews the project, not whatever tree cwd is in.
        cwd = (
            os.environ.get("CLAUDE_WORKING_DIRECTORY", os.getcwd())
            if sandbox == "workspace-write"
            else str(crew_base())
        )

        prompt_bytes = len(prompt.encode("utf-8"))
        if prompt_bytes > _ARG_MAX_BYTES:
            return make_continuation_result(
                ok=False,
                output="",
                error=(f"Prompt too large for subprocess seat ({prompt_bytes} bytes "
                       f"> {_ARG_MAX_BYTES} byte cap). Use a shorter prompt."),
                failure="error",
            )

        cmd = ["agent", "--print"]
        if resume_id:
            cmd += ["--resume", resume_id]
        # read-only review posture: --mode plan applies NO edits (verified) while
        # still allowing read-only shell (git diff) + producing review output.
        if sandbox != "workspace-write":
            cmd += ["--mode", "plan"]
        elif dispatch_options:
            # Write-mode-only dispatch tuning (the structural gate keeping a
            # read-only argv byte-identical even with a [dispatch.cursor] table
            # configured). Inserted at THIS branch point, before the fixed tail
            # block below, because the prompt must stay the LAST positional argv
            # element (unlike codex, whose prompt travels via stdin). False
            # emits nothing (byte-identical to unconfigured, never --force=false).
            # Values arrive pre-validated from the config getter.
            if dispatch_options.get("force") is True:
                cmd += ["--force"]
            if dispatch_options.get("approve_mcps") is True:
                cmd += ["--approve-mcps"]
        if capable:
            cmd += ["--output-format", "stream-json"]
        cmd += [
            "--sandbox", "enabled", "--trust",
            "--workspace", cwd, "--model", chosen_model, prompt,
        ]
        # Shared reaped runner: start_new_session + SIGTERM→SIGKILL killpg
        # teardown on timeout so a hung cursor agent can't orphan billable
        # grandchildren. OSError (launch failure) preserved as before.
        try:
            result = run_reaped(cmd, timeout=timeout, cwd=cwd)
        except OSError as exc:
            return make_continuation_result(
                ok=False, output="", error=f"Failed to launch agent: {exc}", failure="error"
            )
        if result is TIMEOUT:
            return make_continuation_result(
                ok=False,
                output="",
                error=f"Cursor Agent timed out after {timeout}s",
                failure="timeout",
            )
        returncode, proc_stdout, proc_stderr = result

        if capable:
            stream = self._parse_stream(proc_stdout)
            reported_id = (
                stream.mismatched_id
                if stream.ids_mismatched
                else stream.session_id
            )
            if stream.terminal_seen:
                output = stream.terminal_result if stream.terminal_success else ""
            else:
                output = stream.assistant_deltas
            output = _strip_ansi(output).strip()
            stderr_clean = _strip_ansi(proc_stderr).strip()
            auth_marker = _auth_failure_marker(output + stderr_clean)
            if auth_marker is not None:
                return make_continuation_result(
                    ok=False,
                    output=output,
                    error=(f"Cursor Agent authentication required (detected: {auth_marker!r}). "
                           "Sign in at cursor.com/login."),
                    failure="error",
                )
            if returncode != 0:
                return make_continuation_result(
                    ok=False,
                    output="",
                    error=stderr_clean or f"agent exited with code {returncode}",
                    failure="error",
                    conversation_id=reported_id,
                )

            if not output:
                return make_continuation_result(
                    ok=False,
                    output="",
                    error=("agent returned empty stream result at exit 0"
                           + (f"; stderr: {stderr_clean[:500]}" if stderr_clean else "")),
                    failure="error",
                    conversation_id=reported_id,
                )

            failure = "none"
            if (
                not stream.terminal_seen
                or not stream.terminal_success
                or stream.ids_mismatched
                or (
                    phase == "resume" and stream.session_id != resume_id
                )
            ):
                failure = "error"
            persisted_id = stream.session_id if failure == "none" else None
            return make_continuation_result(
                ok=True,
                output=output,
                error=None,
                failure=failure,
                conversation_id=reported_id,
                continuation_id=persisted_id,
            )

        raw_output = _strip_ansi(proc_stdout)
        combined_lower = (raw_output + proc_stderr).lower()
        auth_marker = _auth_failure_marker(combined_lower)
        if auth_marker is not None:
            return make_continuation_result(
                ok=False, output=raw_output,
                error=(f"Cursor Agent authentication required (detected: {auth_marker!r}). "
                       "Sign in at cursor.com/login."),
                failure="error",
            )
        if returncode != 0:
            return make_continuation_result(
                ok=False, output=raw_output,
                error=(f"agent exited with code {returncode}. "
                       f"stderr: {_strip_ansi(proc_stderr)[:500]}"),
                failure="error",
            )
        # Empty output at exit 0 is NOT a valid review (mirrors codex/agy): an
        # all-empty panel must not be rendered as an OK "(no output)" review.
        if not raw_output.strip():
            stderr_clean = _strip_ansi(proc_stderr).strip()
            return make_continuation_result(
                ok=False, output="",
                error=("agent returned empty output at exit 0"
                       + (f"; stderr: {stderr_clean[:500]}" if stderr_clean else "")),
                failure="error",
            )
        return make_continuation_result(
            ok=True, output=raw_output, error=None,
            continuation_id=None,
        )


# =============================================================================
# To add another Cursor model as a panel seat:
#   1. Add a [seats.cursor-<x>] table to multiagent/seats.toml with
#      provider = "cursor" and the model string (`agent models` lists them);
#      opt_in = true keeps it out of the default panels. A DEFAULT seat also needs
#      its entry in the [panels] full list, which the drift guard pins.
#   2. That's it. The registry builds every executor-bearing catalog row, the
#      engine's subprocess-seat allowlist is derived from the registry, the
#      `cursor` group token picks the seat up, and `--seats cursor-<x>` works.
# =============================================================================
