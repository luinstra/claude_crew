"""CodexProvider — drives ``codex exec`` as a subprocess seat.

One codex binary exposes several models; we run more than one as distinct panel
seats. Each seat is a ``CodexProvider`` instance pinned to a model string; the
seats themselves live in the catalog (``multiagent/seats.toml``, or a user's own
config), and the registry builds one instance per catalog row.

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

from state_discovery import crew_base  # the ONE `.crew`/project-root resolver

from . import Provider, ProviderResult
from ._proc import TIMEOUT, run_reaped

# Re-pinned in code because --ignore-user-config drops the config's own value;
# the seat's [seats.<name>].reasoning_effort overrides it through the catalog.
DEFAULT_REASONING_EFFORT = "xhigh"


class CodexProvider(Provider):
    """A panel seat backed by the codex CLI subprocess.

    Pinned to one model. Multiple named instances run as distinct seats (the codex
    rows of the catalog): the seat's name, model, and reasoning_effort all arrive
    from its ``SeatSpec`` through the registry factory, so a seat declared in a
    user's config is a first-class codex seat.
    """

    # EXPLICIT opt-in (fail-CLOSED ABC default is False): codex honors
    # workspace-write natively via its --sandbox arg, so it is a valid
    # /crew:dispatch write seat.
    supports_workspace_write = True

    # Write-mode dispatch tuning, declared on THIS class (never inherited from
    # the ABC's shared default). Single source: the config validator, the
    # `crew dispatch --options` listing, and the scaffold-config comments all
    # read this dict. Value shape: (expected_type, help); str/bool only.
    DISPATCH_OPTIONS = {
        "profile": (str, "codex exec --profile <name>: layers "
                         "$CODEX_HOME/<name>.config.toml over the base user config"),
    }

    def __init__(
        self,
        name: str = "codex",
        default_model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        self.name = name
        self._default_model = default_model
        self._reasoning_effort = reasoning_effort

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
        dispatch_options: dict | None = None,
    ) -> ProviderResult:
        start = time.monotonic()

        # Precedence: explicit model (CLI --model) > the seat's resolved model
        # (the catalog already folded the config layers over the shipped pin).
        # Unlike cursor, a None result is not an error: codex falls back to its
        # own CLI default when --model is omitted.
        chosen_model = model or self._default_model

        # The -o temp file receives the clean final message.
        fd, out_path = tempfile.mkstemp(prefix="crew_codex_", suffix=".txt")
        os.close(fd)

        # --ignore-user-config: hermetic review seat — skip the user's
        # ~/.codex/config.toml (MCP servers, skills, RTK, AGENTS.md preamble).
        # Cuts ~40k startup tokens and keeps the seat reproducible. Because that
        # also drops the config's model_reasoning_effort, we RE-PIN it in code
        # via -c so reviews keep deep reasoning without the user-config bloat.
        # reasoning_effort: the seat's own tune (the catalog resolved the config
        # layers), else the built-in default.
        effort = self._reasoning_effort or DEFAULT_REASONING_EFFORT
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
        # Dispatch options apply in WRITE MODE ONLY (the structural gate that
        # keeps every read-only review argv byte-identical even when a
        # [dispatch.codex] table is configured). --profile is write-mode
        # coherent: write mode loads the user config (--ignore-user-config
        # omitted above), which is the base the profile file layers onto, and
        # the explicit -c model_reasoning_effort pin still wins over
        # profile-supplied config (codex precedence: -c > profile > base).
        # Placement is free here (prompt travels via stdin, unlike cursor's
        # trailing positional prompt), so the pair sits with the other -c/flag
        # tuning. Values arrive pre-validated from the config getter.
        if sandbox == "workspace-write" and dispatch_options:
            profile = dispatch_options.get("profile")
            if profile:
                argv += ["--profile", profile]
        if chosen_model:
            argv += ["--model", chosen_model]
        argv += ["--skip-git-repo-check", "-o", out_path]

        # Workspace-write cwd pin: codex inherits the engine process
        # cwd by default, which can diverge from the guard's repo_dir. In write
        # mode pin the subprocess cwd to CLAUDE_WORKING_DIRECTORY (the SAME
        # expression cmd_dispatch uses for repo_dir) so the seat edits the tree
        # the dispatch guard inspects. A READ-ONLY review seat pins to
        # crew_base() (the project root the run dir + snapshot anchor to), so a
        # divergent process cwd cannot make the seat inspect the wrong repo.
        run_cwd = (
            (os.environ.get("CLAUDE_WORKING_DIRECTORY") or os.getcwd())
            if sandbox == "workspace-write"
            else str(crew_base())
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
