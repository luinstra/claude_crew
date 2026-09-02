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
    agent --print --mode plan --output-format stream-json --sandbox enabled --trust --workspace <cwd> --model <model> <prompt>
``--mode plan`` is the read-only enforcement (see ``run``): it applies no edits,
which plain ``--print`` does NOT guarantee. The stream-json output captures the
runtime-reported model; both stream branches use the terminal result when one
is present, otherwise joined assistant text, with ANSI removed and no other
normalization. The text printer's trailing linefeed is not synthesized. Every
read-only run pays one ``agent --help`` capability probe, capped at 10 seconds
and memoized per process; that probe time is excluded from the seat's elapsed
value. A binary whose help advertises stream-json but prints plain text at exit
0 falls back to raw text with no reported model; terminal error results and
mixed-session streams fail the seat with named diagnostics.
Dropped only for workspace-write.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
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

# Per-process help capability memo, keyed by the resolved agent binary.
_HELP_PROBE_CACHE: dict[str, "_HelpFacts"] = {}
PROBE_TIMEOUT = 10


@dataclass(frozen=True)
class _HelpFacts:
    resume: bool
    output_format: bool
    stream_json: bool


@dataclass(frozen=True)
class _CursorStream:
    session_id: str | None
    mismatched_id: str | None
    ids_mismatched: bool
    model: str | None
    terminal_seen: bool
    terminal_success: bool
    terminal_result: str
    terminal_error: str
    assistant_deltas: str
    events: int


@dataclass(frozen=True)
class _StreamClassification:
    output: str
    error: str | None
    auth_failure: bool = False


_valid_resume_id = valid_conversation_id


def _reset_probe_cache_for_tests() -> None:
    # Test-only reset for isolation of the module-level probe cache.
    _HELP_PROBE_CACHE.clear()


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


def _stream_text(stream: _CursorStream) -> str:
    """Return terminal result text, or joined assistant text without trimming."""
    text = stream.terminal_result if stream.terminal_seen else stream.assistant_deltas
    return _strip_ansi(text)


def _has_json_object_line(stdout: str) -> bool:
    """Return whether any nonempty stdout line parses as a JSON object."""
    for line in (stdout or "").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(event, dict):
            return True
    return False


def _looks_like_json_payload(stdout: str) -> bool:
    """Return whether stdout is JSON that produced no recognized events.

    Only real parses count: the whole stripped stdout as one object or array
    (a pretty-printed object or a top-level array never parses line by line),
    or any single line as an object. A first-character guess would also match
    a plain-text review that opens with a bracketed finding tag."""
    text = (stdout or "").strip()
    if not text:
        return False
    try:
        whole = json.loads(text)
    except (TypeError, ValueError):
        whole = None
    if isinstance(whole, (dict, list)):
        return True
    return _has_json_object_line(stdout)


def _classify_stream(
    stream: _CursorStream,
    *,
    returncode: int,
    stdout: str,
    stderr: str,
) -> _StreamClassification:
    """Apply one failure and output policy to every parsed Cursor stream."""
    output = _stream_text(stream)
    stderr_clean = _strip_ansi(stderr).strip()
    scan_text = output if stream.events else _strip_ansi(stdout)
    auth_marker = _auth_failure_marker(
        scan_text + stderr_clean + stream.terminal_error
    )
    if auth_marker is not None:
        return _StreamClassification(
            output=output,
            error=(
                f"Cursor Agent authentication required (detected: {auth_marker!r}). "
                "Sign in at cursor.com/login."
            ),
            auth_failure=True,
        )
    if returncode != 0:
        return _StreamClassification(
            output=output,
            error=(
                f"agent exited with code {returncode}. "
                f"stderr: {stderr_clean[:500]}"
            ),
        )
    if stream.events == 0 and stdout.strip():
        if _looks_like_json_payload(stdout):
            return _StreamClassification(
                output="",
                error=(
                    "no recognized stream-json events at exit 0; stdout head: "
                    f"{_strip_ansi(stdout)[:200]!r}"
                ),
            )
        return _StreamClassification(output=_strip_ansi(stdout), error=None)
    if stream.terminal_seen and not stream.terminal_success:
        error = "agent reported an error result at exit 0"
        if stream.terminal_error:
            error += f": {stream.terminal_error[:500]}"
        if stderr_clean:
            error += f"; stderr: {stderr_clean[:500]}"
        return _StreamClassification(output="", error=error)
    if not output.strip():
        error = "agent returned empty output at exit 0"
        if stderr_clean:
            error += f"; stderr: {stderr_clean[:500]}"
        return _StreamClassification(output="", error=error)
    return _StreamClassification(output=output, error=None)


def _stream_identity_error(stream: _CursorStream) -> str:
    """Describe a stream that mixed events from more than one session."""
    return (
        "agent stream session ID mismatch: "
        f"expected {stream.session_id!r}, encountered {stream.mismatched_id!r}"
    )


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
        facts = self._probe_help(os.path.realpath(path))
        return facts.resume and facts.output_format and facts.stream_json

    def _supports_stream_json_runtime(self) -> bool:
        path = shutil.which("agent")
        if not path:
            return False
        facts = self._probe_help(os.path.realpath(path))
        return facts.output_format and facts.stream_json

    @staticmethod
    def _probe_help(path: str) -> _HelpFacts:
        cached = _HELP_PROBE_CACHE.get(path)
        if cached is not None:
            return cached
        try:
            probe = run_reaped([path, "--help"], timeout=PROBE_TIMEOUT)
            if probe is TIMEOUT:
                print(
                    f"warning: agent --help probe timed out after {PROBE_TIMEOUT}s; "
                    "stream-json capture and continuation are disabled for this process",
                    file=sys.stderr,
                )
                facts = _HelpFacts(False, False, False)
                _HELP_PROBE_CACHE[path] = facts
                return facts
            returncode, stdout, stderr = probe
            help_text = (stdout or "") + (stderr or "")
            facts = _HelpFacts(
                resume=returncode == 0 and "--resume" in help_text,
                output_format=returncode == 0 and "--output-format" in help_text,
                stream_json=returncode == 0 and "stream-json" in help_text,
            )
            if returncode != 0:
                print(
                    f"warning: agent --help exited with code {returncode}; "
                    "stream-json capture and continuation are disabled for this process",
                    file=sys.stderr,
                )
            elif not (facts.output_format and facts.stream_json):
                # The cache makes this a once-per-process note, like the others.
                print(
                    "warning: agent --help does not advertise --output-format "
                    "stream-json; stream-json capture and continuation are "
                    "disabled for this process (both read the same flags)",
                    file=sys.stderr,
                )
            _HELP_PROBE_CACHE[path] = facts
            return facts
        except OSError as exc:
            print(
                f"warning: agent --help probe failed to launch: {exc}; "
                "stream-json capture and continuation are disabled for this process",
                file=sys.stderr,
            )
            facts = _HelpFacts(False, False, False)
            _HELP_PROBE_CACHE[path] = facts
            return facts

    @staticmethod
    def _parse_stream(stdout: str) -> _CursorStream:
        session_id: str | None = None
        mismatched_id: str | None = None
        ids_mismatched = False
        initialized = False
        model: str | None = None
        terminal_seen = False
        terminal_success = False
        terminal_result = ""
        terminal_error = ""
        assistant_deltas: list[str] = []
        events = 0

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
                    reported = event.get("model")
                    if isinstance(reported, str) and reported:
                        model = reported

            if not (is_init or is_assistant or is_result):
                continue
            events += 1

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
                else:
                    terminal_error = (
                        _text_from_value(event.get("result"))
                        or _text_from_value(event.get("error"))
                    )

        return _CursorStream(
            session_id=session_id,
            mismatched_id=mismatched_id,
            ids_mismatched=ids_mismatched,
            model=model,
            terminal_seen=terminal_seen,
            terminal_success=terminal_success,
            terminal_result=terminal_result,
            terminal_error=terminal_error,
            assistant_deltas="".join(assistant_deltas),
            events=events,
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

        ``sandbox="read-only"`` (the default, and what standalone review and
        question seats use) runs the agent in ``--mode plan``: analyse + read-only shell (it CAN
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
        stream_enabled = False
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
            reported_model: str | None = None,
        ) -> ProviderResult:
            values = {
                "name": self.name,
                "model": chosen_model,
                "ok": ok,
                "output": output,
                "error": error,
                "elapsed": time.monotonic() - start,
                "reported_model": reported_model,
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
        if sandbox != "workspace-write":
            probe_start = time.monotonic()
            stream_supported = self._supports_stream_json_runtime()
            start += time.monotonic() - probe_start
        else:
            stream_supported = False
        stream_enabled = capable or stream_supported
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
        if stream_enabled:
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
            classification = _classify_stream(
                stream,
                returncode=returncode,
                stdout=proc_stdout,
                stderr=proc_stderr,
            )
            if classification.error is not None:
                return make_continuation_result(
                    ok=False,
                    output=classification.output,
                    error=classification.error,
                    failure="error",
                    conversation_id=(
                        None if classification.auth_failure else reported_id
                    ),
                    reported_model=stream.model,
                )
            output = classification.output

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
                reported_model=stream.model,
            )

        if stream_enabled:
            parsed = self._parse_stream(proc_stdout)
            if parsed.ids_mismatched:
                return make_continuation_result(
                    ok=False,
                    output="",
                    error=_stream_identity_error(parsed),
                    failure="error",
                    reported_model=None,
                )
            classification = _classify_stream(
                parsed,
                returncode=returncode,
                stdout=proc_stdout,
                stderr=proc_stderr,
            )
            if classification.error is not None:
                return make_continuation_result(
                    ok=False,
                    output=classification.output,
                    error=classification.error,
                    failure="error",
                    reported_model=parsed.model,
                )
            return make_continuation_result(
                ok=True,
                output=classification.output,
                error=None,
                continuation_id=None,
                reported_model=parsed.model,
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
#      via = ["cursor"] and the model string (`agent models` lists them);
#      opt_in = true keeps it out of the default panels. A DEFAULT seat also needs
#      its entry in the [panels] full list, which the drift guard pins.
#   2. That's it. The registry builds every executor-bearing catalog row, the
#      engine's subprocess-seat allowlist is derived from the registry, the
#      `cursor` group token picks the seat up, and `--seats cursor-<x>` works.
# =============================================================================
