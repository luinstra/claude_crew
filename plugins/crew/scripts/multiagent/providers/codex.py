"""CodexProvider — drives ``codex exec`` as a subprocess seat.

Invocation (verified against codex-cli 0.142.2 `codex exec --help`):
    codex exec - --ignore-user-config --sandbox <lvl> [--model X] --skip-git-repo-check -o <tmpfile>
Prompt is delivered via STDIN (the ``-`` arg). The final agent message is read
from the ``-o`` temp file (avoids stderr progress noise). stderr is captured
into ``error`` on nonzero exit (this is also the auth-failure path).

``--ignore-user-config`` makes the review seat HERMETIC: it skips the user's
``~/.codex/config.toml`` — their MCP servers (e.g. Neon), skills, RTK, and
``AGENTS.md`` preamble — so a review seat doesn't drag ~40k tokens of unrelated
personal-environment startup into every run, and executes in a clean,
reproducible default config instead of whatever happens to be in ``~/.codex``.
NOTE: this ALSO resets the config's ``model_reasoning_effort`` (e.g. ``xhigh``)
to codex's default — add ``-c model_reasoning_effort=<level>`` to the argv if a
review needs deeper reasoning than the default.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time

from . import Provider, ProviderResult


class CodexProvider(Provider):
    name = "codex"

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
        # ~/.codex/config.toml (MCP servers, skills, RTK, AGENTS.md preamble +
        # model_reasoning_effort). Cuts ~40k startup tokens and keeps the seat
        # reproducible. (See module docstring for the reasoning-effort note.)
        argv = ["codex", "exec", "-", "--ignore-user-config", "--sandbox", sandbox]
        if model:
            argv += ["--model", model]
        argv += ["--skip-git-repo-check", "-o", out_path]

        try:
            try:
                proc = subprocess.run(
                    argv,
                    input=prompt,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
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
