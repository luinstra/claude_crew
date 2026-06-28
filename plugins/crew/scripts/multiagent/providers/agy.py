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
    POSTURE (accepted): for a local, single-author repo reviewing its own
    commits the in-workspace-write residual is accepted. REVISIT if this ever
    reviews untrusted / external-contributor diffs — then run agy in a disposable
    git worktree or copy for true isolation (or make it opt-in for code review).
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

from multiagent import config

from . import Provider, ProviderResult


# Conservative cap well under macOS ARG_MAX (~1 MB) to leave headroom for the
# rest of argv + the environment block.
AGY_PROMPT_MAX_BYTES = 256 * 1024  # 256 KB

# Default print-timeout when [seats.agy].print_timeout is unset (the sole 8m source).
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

# An agy auth-failure banner REPLACES the review: it is short and carries no
# review structure. A genuine review — INCLUDING a review OF auth code, whose
# prose legitimately quotes these very markers — is long and/or structured. We
# only treat marker-bearing output as an auth failure when it does NOT look like
# a real review, so reviewing auth code no longer false-positives (which would
# silently drop the agy seat).
AUTH_BANNER_MAX_CHARS = 1000

# Tokens distinctive to our review/discuss prompts (review rubric + discuss
# stance). Chosen NOT to collide with auth-banner wording — so they must be
# bracketed tags or distinctive multi-word headers, NEVER bare English verdict
# words. We deliberately EXCLUDE "approved"/"revise" (an auth/account banner can
# say "not approved" / "please revise your credentials" → would mask a real
# banner = false-negative) and "pass"/"fail" ("authentication failed"). A
# compliant review still trips this via its bracketed findings, "confidence"
# line, or discuss headers; if not, length carries it.
_REVIEW_STRUCTURE_MARKERS = (
    "[blocking]",
    "[minor]",
    # The rubric emits "CONFIDENCE: <level>" — match the colon form so an auth
    # banner saying "confidence level: LOW — please authenticate" (no colon
    # right after the word) does NOT masquerade as a review (false-negative).
    "confidence:",
    "direct take",
    "strongest objection",
    "risks / tradeoff",
)


def parse_timeout_to_seconds(value: str | None, default_seconds: float = 480.0) -> float:
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


def _looks_like_review(low: str) -> bool:
    """True if the output reads like a real review/take, not an auth banner.

    A genuine seat response is long and/or carries our review-structure tokens;
    the auth-failure banner is short and structureless. This is what lets a
    review OF auth code (which quotes the auth markers) through without a
    false positive.
    """
    return (
        len(low.strip()) > AUTH_BANNER_MAX_CHARS
        or any(tok in low for tok in _REVIEW_STRUCTURE_MARKERS)
    )


def detect_auth_or_error(cleaned: str) -> str | None:
    """Return a diagnostic if ``cleaned`` output looks like auth/error noise.

    Catches the live failure mode: agy returns an OAuth URL +
    "authentication timed out" with EXIT 0. Returns None when the output looks
    like a real review — including a review that merely DISCUSSES auth (long
    and/or structured output is trusted even when it quotes the auth markers).
    """
    low = cleaned.lower()
    has_auth = any(marker in low for marker in _AUTH_MARKERS) or (
        # A bare login/auth URL banner with little else.
        "http" in low and ("auth" in low or "login" in low or "accounts.google" in low)
    )
    if not has_auth:
        return None
    # Don't flag a real review that merely discusses auth code.
    if _looks_like_review(low):
        return None
    return "agy authentication required / auth output detected (not a review)"


class AgyProvider(Provider):
    name = "agy"
    # EXPLICIT opt-in (fail-CLOSED ABC default is False): agy is a valid
    # /crew:dispatch write seat. NUANCE — agy has a SINGLE confined --sandbox
    # mode (it cannot express codex's read-only vs workspace-write split), so the
    # ``sandbox`` arg is a no-op for the CLI: agy ALWAYS allows in-workspace
    # writes and ALWAYS blocks out-of-workspace. It honors workspace-write in the
    # only way it can. The cwd pin below still applies in write mode.
    supports_workspace_write = True

    def is_available(self) -> tuple[bool, str]:
        if shutil.which("agy"):
            return (True, "")
        return (False, "agy not found on PATH")

    def _resolved_model(self, model: str | None) -> str:
        # Precedence: explicit model (CLI) > per-repo .crew/config.toml > global
        # ~/.crew-config.toml ([seats.agy].model, both via config.seat_model) >
        # built-in Gemini default.
        if model:
            return model
        return config.seat_model("agy") or "Gemini 3.1 Pro (High)"

    def _print_timeout(self) -> str:
        # Precedence: per-repo .crew/config.toml > global ~/.crew-config.toml
        # ([seats.agy].print_timeout, both via config.agy_print_timeout) >
        # built-in 8m default.
        return config.agy_print_timeout() or DEFAULT_PRINT_TIMEOUT

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

        # Workspace-write cwd pin (closes R13): agy inherits the engine process
        # cwd by default, which can diverge from the dispatch guard's repo_dir. In
        # write mode pin the subprocess cwd to CLAUDE_WORKING_DIRECTORY (the SAME
        # expression cmd_dispatch uses for repo_dir) so the seat edits the tree
        # the guard inspects. Read-only passes NO cwd (None) — review/debate is
        # byte-for-byte unchanged.
        run_cwd = (
            (os.environ.get("CLAUDE_WORKING_DIRECTORY") or os.getcwd())
            if sandbox == "workspace-write"
            else None
        )

        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,  # own process group, so we can reap children
            cwd=run_cwd,
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
