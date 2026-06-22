"""AgyProvider — drives the Antigravity ``agy`` CLI as a subprocess seat.

Verified against agy 1.0.10 ``--help``. Invocation:

    ["agy", "-p", "<prompt-as-its-own-positional-element>",
     "--model", "Gemini 3.1 Pro (High)",
     "--print-timeout", "8m",
     "--sandbox"]

Key facts (do NOT regress these):
  * ``-p``/``--print`` is the BOOLEAN print-mode flag. It does NOT take the
    prompt as its value. The prompt is a SEPARATE positional argv element.
  * ``--sandbox`` (NOT ``--dangerously-skip-permissions``) is how we run
    headless. A review only needs to run git + read files (in-workspace),
    which the sandbox auto-allows WITHOUT a permission prompt (so print mode
    does not hang). What the sandbox BLOCKS is the dangerous part:
    out-of-workspace writes/exec. ``--dangerously-skip-permissions`` would
    instead auto-approve EVERYTHING system-wide — wrong for a read-only review
    and a real prompt-injection surface (adversarial diff content steering an
    unconfined agent). Empirically verified: under ``--sandbox`` agy runs
    ``git status`` fine but an out-of-workspace write to ``/tmp`` is blocked.
    (Residual: in-workspace writes are still allowed, but those land in git and
    are recoverable — a vastly smaller blast radius than a system-wide bypass.)
  * ``stdin=subprocess.DEVNULL``. No ``pty``/``script`` wrapper, no
    ``--prompt-file`` (that flag does not exist). The earlier
    ``script: write master: I/O error`` was a prompt-file-into-stdin bug.
  * stdout carries ANSI escape codes — strip them.
  * ``--model`` is SET to a Gemini model (unset can route to a Claude/GPT
    model -> wrong quota group -> empty returns).
  * Wall-clock timeout is derived from / >= ``--print-timeout`` (default 8m),
    NOT the ABC's generic 300s.
  * Failure detection: empty output OR non-empty auth/error output (even at
    exit 0) -> ok=False. Exit code alone is NOT trusted.
  * ARG_MAX: the prompt is one positional argv element with no stdin fallback;
    size-check it and degrade loudly above a conservative cap.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import time

from . import Provider, ProviderResult


# Conservative cap well under macOS ARG_MAX (~1 MB) to leave headroom for the
# rest of argv + the environment block.
AGY_PROMPT_MAX_BYTES = 256 * 1024  # 256 KB

# Default print-timeout if CREW_MA_AGY_PRINT_TIMEOUT is unset.
DEFAULT_PRINT_TIMEOUT = "8m"

# Grace margin (seconds) added on top of --print-timeout for teardown so the
# wall-clock floor sits just above agy's own print deadline.
TEARDOWN_GRACE_SECONDS = 5

# Hard deadline (seconds) for reaping after SIGTERM/SIGKILL so a hung
# invocation can't pin its worker thread indefinitely.
REAP_DEADLINE_SECONDS = 10

# ANSI escape sequence stripper.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

# Auth / error markers that mean "not a review" even at exit 0.
_AUTH_MARKERS = (
    "authentication timed out",
    "please authenticate",
    "please sign in",
    "sign in to continue",
    "you are not logged in",
    "not authenticated",
    "authentication required",
    "log in to continue",
    "oauth",
    "https://accounts.google.com",
)


def parse_timeout_to_seconds(value: str, default_seconds: float = 480.0) -> float:
    """Parse an agy-style timeout (e.g. ``8m``, ``480s``, ``480``) to seconds."""
    if value is None:
        return default_seconds
    s = str(value).strip().lower()
    if not s:
        return default_seconds
    try:
        if s.endswith("ms"):
            return float(s[:-2]) / 1000.0
        if s.endswith("m"):
            return float(s[:-1]) * 60.0
        if s.endswith("h"):
            return float(s[:-1]) * 3600.0
        if s.endswith("s"):
            return float(s[:-1])
        return float(s)
    except ValueError:
        return default_seconds


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def detect_auth_or_error(cleaned: str) -> str | None:
    """Return a diagnostic if ``cleaned`` output looks like auth/error noise.

    Catches the live failure mode: agy returns an OAuth URL +
    "authentication timed out" with EXIT 0. Returns None when the output looks
    like a real review.
    """
    low = cleaned.lower()
    for marker in _AUTH_MARKERS:
        if marker in low:
            return "agy authentication required / auth output detected (not a review)"
    # A bare login/auth URL banner with little else.
    if "http" in low and ("auth" in low or "login" in low or "accounts.google" in low):
        return "agy authentication required / auth output detected (not a review)"
    return None


class AgyProvider(Provider):
    name = "agy"

    def is_available(self) -> tuple[bool, str]:
        if shutil.which("agy"):
            return (True, "")
        return (False, "agy not found on PATH")

    def _resolved_model(self, model: str | None) -> str:
        if model:
            return model
        return os.environ.get("CREW_MA_AGY_MODEL", "Gemini 3.1 Pro (High)")

    def _print_timeout(self) -> str:
        return os.environ.get("CREW_MA_AGY_PRINT_TIMEOUT", DEFAULT_PRINT_TIMEOUT)

    def effective_timeout(self, timeout: int) -> float:
        """Wall-clock timeout >= --print-timeout (BLOCKING #1).

        ``max(caller_timeout, print_timeout_seconds + grace)`` so an explicitly
        larger caller timeout still wins, but the floor is never below
        ``--print-timeout``. The ABC's generic 300s is NOT a cap on agy.
        """
        print_secs = parse_timeout_to_seconds(self._print_timeout())
        floor = print_secs + TEARDOWN_GRACE_SECONDS
        return max(float(timeout), floor)

    def run(
        self,
        prompt: str,
        *,
        sandbox: str = "read-only",
        model: str | None = None,
        timeout: int = 300,
    ) -> ProviderResult:
        start = time.monotonic()
        resolved_model = self._resolved_model(model)

        # ARG_MAX size-check (should-fix #4): the prompt is one positional argv
        # element with NO stdin/--prompt-file fallback. Degrade loudly rather
        # than risk a truncated/failed execve.
        prompt_bytes = len(prompt.encode("utf-8"))
        if prompt_bytes > AGY_PROMPT_MAX_BYTES:
            return ProviderResult(
                name=self.name,
                model=resolved_model,
                ok=False,
                output="",
                error=(
                    f"agy prompt exceeds safe ARG_MAX threshold "
                    f"({prompt_bytes} bytes > {AGY_PROMPT_MAX_BYTES}); "
                    f"skipping agy seat to avoid a truncated/failed exec"
                ),
                elapsed=time.monotonic() - start,
            )

        # -p and the prompt are DISTINCT argv elements; prompt is positional.
        # ``--sandbox`` confines agy (out-of-workspace writes/exec blocked) while
        # still auto-allowing the in-workspace git + file reads a review needs —
        # NOT ``--dangerously-skip-permissions`` (system-wide auto-approve). The
        # ``sandbox`` argument is accepted for Provider-ABC parity with codex;
        # agy's ``--sandbox`` is a single confinement mode (it cannot express
        # codex's read-only vs workspace-write split), so any non-empty value
        # maps to confined, and we never run agy unconfined.
        argv = [
            "agy",
            "-p",
            prompt,
            "--model",
            resolved_model,
            "--print-timeout",
            self._print_timeout(),
            "--sandbox",
        ]

        wall_clock = self.effective_timeout(timeout)

        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,  # own process group, so we can reap children
        )

        try:
            stdout, stderr = proc.communicate(timeout=wall_clock)
        except subprocess.TimeoutExpired:
            # SIGTERM -> SIGKILL -> hard-deadline waitpid so a hung invocation
            # cannot pin its worker thread.
            self._terminate(proc)
            return ProviderResult(
                name=self.name,
                model=resolved_model,
                ok=False,
                output="",
                error=f"agy timed out after {wall_clock:.0f}s (wall clock)",
                elapsed=time.monotonic() - start,
            )

        elapsed = time.monotonic() - start
        cleaned = strip_ansi(stdout or "").strip()

        # Empty output -> failure.
        if not cleaned:
            err = strip_ansi(stderr or "").strip()
            return ProviderResult(
                name=self.name,
                model=resolved_model,
                ok=False,
                output="",
                error="agy returned no output" + (f": {err}" if err else ""),
                elapsed=elapsed,
            )

        # Auth/error output (even at exit 0) -> failure (BLOCKING #2).
        auth_err = detect_auth_or_error(cleaned)
        if auth_err is not None:
            return ProviderResult(
                name=self.name,
                model=resolved_model,
                ok=False,
                output="",
                error=auth_err,
                elapsed=elapsed,
            )

        # Nonzero exit with real output -> still a failure, capture diagnostic.
        if proc.returncode != 0:
            err = strip_ansi(stderr or "").strip()
            return ProviderResult(
                name=self.name,
                model=resolved_model,
                ok=False,
                output="",
                error=err or f"agy exited with status {proc.returncode}",
                elapsed=elapsed,
            )

        return ProviderResult(
            name=self.name,
            model=resolved_model,
            ok=True,
            output=cleaned,
            error=None,
            elapsed=elapsed,
        )

    def _terminate(self, proc: subprocess.Popen) -> None:
        """SIGTERM -> SIGKILL -> hard-deadline reap of the process group."""
        try:
            pgid = os.getpgid(proc.pid)
        except (ProcessLookupError, OSError):
            pgid = None

        def _signal(sig: int) -> None:
            try:
                if pgid is not None:
                    os.killpg(pgid, sig)
                else:
                    proc.send_signal(sig)
            except (ProcessLookupError, OSError):
                pass

        _signal(signal.SIGTERM)
        try:
            proc.wait(timeout=REAP_DEADLINE_SECONDS / 2)
            return
        except subprocess.TimeoutExpired:
            pass

        _signal(signal.SIGKILL)
        try:
            proc.wait(timeout=REAP_DEADLINE_SECONDS / 2)
        except subprocess.TimeoutExpired:
            # Last resort: don't block the worker thread forever.
            pass
