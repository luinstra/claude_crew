"""CodexProvider — drives ``codex exec`` as a subprocess seat.

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
import subprocess
import tempfile
import time

from multiagent import config

from . import Provider, ProviderResult


class CodexProvider(Provider):
    name = "codex"
    # EXPLICIT opt-in (fail-CLOSED ABC default is False): codex honors
    # workspace-write natively via its --sandbox arg, so it is a valid
    # /crew:dispatch write seat.
    supports_workspace_write = True

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

        # The -o temp file receives the clean final message.
        fd, out_path = tempfile.mkstemp(prefix="crew_codex_", suffix=".txt")
        os.close(fd)

        # --ignore-user-config: hermetic review seat — skip the user's
        # ~/.codex/config.toml (MCP servers, skills, RTK, AGENTS.md preamble).
        # Cuts ~40k startup tokens and keeps the seat reproducible. Because that
        # also drops the config's model_reasoning_effort, we RE-PIN it in code
        # via -c so reviews keep deep reasoning without the user-config bloat.
        # reasoning_effort: [seats.codex].reasoning_effort wins (per-repo
        # .crew/config.toml over global ~/.crew-config.toml, both via
        # config.codex_reasoning_effort), else the built-in xhigh default
        # (re-pinned because --ignore-user-config drops the config's own value).
        effort = config.codex_reasoning_effort() or "xhigh"
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
        if model:
            argv += ["--model", model]
        argv += ["--skip-git-repo-check", "-o", out_path]

        # Workspace-write cwd pin (closes R13): codex inherits the engine process
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
            try:
                proc = subprocess.run(
                    argv,
                    input=prompt,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    cwd=run_cwd,
                )
            except subprocess.TimeoutExpired:
                elapsed = time.monotonic() - start
                return ProviderResult(
                    name=self.name,
                    model=model,
                    ok=False,
                    output="",
                    error=f"codex timed out after {timeout}s",
                    elapsed=elapsed,
                )

            elapsed = time.monotonic() - start

            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout or "").strip()
                return ProviderResult(
                    name=self.name,
                    model=model,
                    ok=False,
                    output="",
                    error=err or f"codex exited with status {proc.returncode}",
                    elapsed=elapsed,
                )

            # Read the clean final message from the -o file.
            output = ""
            try:
                with open(out_path, "r", encoding="utf-8", errors="replace") as f:
                    output = f.read().strip()
            except OSError:
                output = (proc.stdout or "").strip()

            if not output:
                # Fall back to stdout, else report empty as a failure.
                output = (proc.stdout or "").strip()
            if not output:
                return ProviderResult(
                    name=self.name,
                    model=model,
                    ok=False,
                    output="",
                    error="codex returned no output",
                    elapsed=elapsed,
                )

            return ProviderResult(
                name=self.name,
                model=model,
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
