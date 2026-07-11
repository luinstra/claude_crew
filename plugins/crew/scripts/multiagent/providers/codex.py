"""CodexProvider — drives ``codex exec`` as a subprocess seat.

One codex binary exposes several models; we run more than one as distinct panel
seats (see ``CODEX_SEATS`` below, mirroring cursor's ``CURSOR_SEATS``). Each
seat is a ``CodexProvider`` instance pinned to a model string; adding a model
is a one-line change to ``CODEX_SEATS`` (the registry and ``--seats`` pick it
up automatically).

Invocation (verified against codex-cli 0.142.2 `codex exec --help`):
    codex exec - --ignore-user-config -c model_reasoning_effort=xhigh \
        --sandbox <lvl> [--model X] --skip-git-repo-check -o <tmpfile>
Prompt is delivered via STDIN (the ``-`` arg). The final agent message is read
from the ``-o`` temp file (avoids stderr progress noise). stderr is captured
into ``error`` on nonzero exit (this is also the auth-failure path).

``--ignore-user-config`` makes the review seat HERMETIC: it skips the user's
``~/.codex/config.toml`` — their MCP servers (e.g. Neon), skills, RTK, and
``AGENTS.md`` preamble — so a review seat doesn't drag ~40k tokens of unrelated
personal-environment startup into every run, and executes in a clean,
reproducible default config instead of whatever happens to be in ``~/.codex``.
Because ignoring the config ALSO drops its ``model_reasoning_effort``, we
RE-PIN it explicitly with ``-c model_reasoning_effort=xhigh`` so reviews keep
deep reasoning WITHOUT the user-config bloat — the value is set in code, not
inherited. (Lower it via this same flag if a faster/cheaper review is wanted.)
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time

from multiagent import config

from . import Provider, ProviderResult
from ._proc import TIMEOUT, run_reaped

# === The codex panel seats: SINGLE SOURCE OF TRUTH ===========================
# name -> model string `codex exec --model` accepts.
# Both are DEFAULT panel seats: two OpenAI voices at different reasoning styles
# is deliberate cross-style coverage on the one codex subscription. Retune a
# seat without code via [seats.<name>].model in .crew/config.toml or the global
# ~/.crew-config.toml.
CODEX_SEATS: dict[str, str] = {
    "codex":      "gpt-5.6-sol",
    "codex-luna": "gpt-5.6-luna",
}


class CodexProvider(Provider):
    """A panel seat backed by the codex CLI subprocess.

    Pinned to one model. Multiple named instances (see ``CODEX_SEATS``) run as
    distinct seats; ``codex`` and ``codex-luna`` are both default panel seats.
    """

    # EXPLICIT opt-in (fail-CLOSED ABC default is False): codex honors
    # workspace-write natively via its --sandbox arg, so it is a valid
    # /crew:dispatch write seat.
    supports_workspace_write = True

    def __init__(
        self,
        name: str = "codex",
        default_model: str | None = None,
    ) -> None:
        self.name = name
        self._default_model = default_model

    def is_available(self) -> tuple[bool, str]:
        # PATH only — no auth probe, no spawning codex.
        if shutil.which("codex"):
            return (True, "")
        return (False, "codex not found on PATH")

    def run(
        self,
        prompt: str,
        *,
        sandbox: str = "read-only",
        model: str | None = None,
        timeout: int = 300,
    ) -> ProviderResult:
        start = time.monotonic()

        # Precedence: explicit model (CLI --model) > per-repo .crew/config.toml >
        # global ~/.crew-config.toml ([seats.<name>].model, both via
        # config.seat_model) > the seat's built-in CODEX_SEATS pin. Unlike
        # cursor, a None result is not an error: codex falls back to its own
        # CLI default when --model is omitted.
        chosen_model = (
            model
            or config.seat_model(self.name)
            or self._default_model
        )

        # The -o temp file receives the clean final message.
        fd, out_path = tempfile.mkstemp(prefix="crew_codex_", suffix=".txt")
        os.close(fd)

        # --ignore-user-config: hermetic review seat — skip the user's
        # ~/.codex/config.toml (MCP servers, skills, RTK, AGENTS.md preamble).
        # Cuts ~40k startup tokens and keeps the seat reproducible. Because that
        # also drops the config's model_reasoning_effort, we RE-PIN it in code
        # via -c so reviews keep deep reasoning without the user-config bloat.
        # reasoning_effort: [seats.<this seat>].reasoning_effort wins (per-repo
        # .crew/config.toml over global ~/.crew-config.toml, both via
        # config.codex_reasoning_effort, resolved per codex seat), else the
        # built-in xhigh default (re-pinned because --ignore-user-config drops
        # the config's own value).
        effort = config.codex_reasoning_effort(self.name) or "xhigh"
        argv = ["codex", "exec", "-"]
        # --ignore-user-config makes a READ-ONLY review seat hermetic (skips
        # ~/.codex/config.toml + AGENTS.md + project conventions/skills, ~40k
        # startup tokens, reproducible). A WORK seat (/crew:dispatch,
        # sandbox=="workspace-write") WANTS that project context so its edits
        # match house style, so we OMIT the flag in write mode (RATIFIED
        # decision). The -c model_reasoning_effort pin stays UNCONDITIONAL so
        # dispatch keeps a deterministic effort even with user config loaded
        # (explicit -c wins). Read-only review/debate is byte-for-byte unchanged.
        if sandbox != "workspace-write":
            argv += ["--ignore-user-config"]
        argv += [
            "-c", f"model_reasoning_effort={effort}",
            "--sandbox", sandbox,
        ]
        if chosen_model:
            argv += ["--model", chosen_model]
        argv += ["--skip-git-repo-check", "-o", out_path]

        # Workspace-write cwd pin: codex inherits the engine process
        # cwd by default, which can diverge from the guard's repo_dir. In write
        # mode pin the subprocess cwd to CLAUDE_WORKING_DIRECTORY (the SAME
        # expression cmd_dispatch uses for repo_dir) so the seat edits the tree
        # the dispatch guard inspects. Read-only passes NO cwd (None) — the
        # review path is byte-for-byte unchanged.
        run_cwd = (
            (os.environ.get("CLAUDE_WORKING_DIRECTORY") or os.getcwd())
            if sandbox == "workspace-write"
            else None
        )

        try:
            # Shared reaped runner: start_new_session + SIGTERM→SIGKILL
            # killpg teardown on timeout so a hung codex can't orphan billable
            # grandchildren. Prompt via stdin (input_text), cwd preserved.
            result = run_reaped(argv, input_text=prompt, timeout=timeout, cwd=run_cwd)
            if result is TIMEOUT:
                elapsed = time.monotonic() - start
                return ProviderResult(
                    name=self.name,
                    model=chosen_model,
                    ok=False,
                    output="",
                    error=f"codex timed out after {timeout}s",
                    elapsed=elapsed,
                )
            returncode, proc_stdout, proc_stderr = result

            elapsed = time.monotonic() - start

            if returncode != 0:
                err = (proc_stderr or proc_stdout or "").strip()
                return ProviderResult(
                    name=self.name,
                    model=chosen_model,
                    ok=False,
                    output="",
                    error=err or f"codex exited with status {returncode}",
                    elapsed=elapsed,
                )

            # Read the clean final message from the -o file.
            output = ""
            try:
                with open(out_path, "r", encoding="utf-8", errors="replace") as f:
                    output = f.read().strip()
            except OSError:
                output = (proc_stdout or "").strip()

            if not output:
                # Fall back to stdout, else report empty as a failure.
                output = (proc_stdout or "").strip()
            if not output:
                return ProviderResult(
                    name=self.name,
                    model=chosen_model,
                    ok=False,
                    output="",
                    error="codex returned no output",
                    elapsed=elapsed,
                )

            return ProviderResult(
                name=self.name,
                model=chosen_model,
                ok=True,
                output=output,
                error=None,
                elapsed=elapsed,
            )
        finally:
            try:
                os.unlink(out_path)
            except OSError:
                pass
