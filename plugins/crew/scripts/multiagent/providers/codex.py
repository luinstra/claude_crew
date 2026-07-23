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

import json
import os
import shutil
import tempfile
import time

from multiagent.continuation_ids import valid_conversation_id
from state_discovery import crew_base  # the ONE `.crew`/project-root resolver

from . import (
    ContinuationOutcome,
    Provider,
    ProviderContinuation,
    ProviderResult,
)
from ._proc import TIMEOUT, run_reaped

# Re-pinned in code because --ignore-user-config drops the config's own value;
# the seat's [seats.<name>].reasoning_effort overrides it through the catalog.
DEFAULT_REASONING_EFFORT = "xhigh"

# Per-process continuation-capability memo, keyed by the RESOLVED codex binary
# path. A dispatch process probes a given binary at most once; a binary upgrade
# is re-probed by the NEXT process, so there is no on-disk cache to stale and no
# invalidation key to maintain.
_CONTINUATION_PROBE_CACHE: dict[str, bool] = {}
# Non-billable probe deadline: `--help` returns immediately, so this only bounds
# a hung/broken binary and never a real turn.
PROBE_TIMEOUT = 10


def _reset_probe_cache_for_tests() -> None:
    """Clear the per-process continuation-probe memo (test-only)."""
    _CONTINUATION_PROBE_CACHE.clear()


_valid_resume_id = valid_conversation_id


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
    # Opt-in: this adapter HAS an exact-ID resume path. It is a COARSE gate only
    # (Agy never sets it); the real per-run decision is the cached runtime probe
    # (_supports_continuation_runtime), which downgrades a binary that lacks the
    # `--json` + `exec resume` surface to a truthfully-reported fresh run.
    supports_continuation = True

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

    def _supports_continuation_runtime(self) -> bool:
        """Cached, NON-BILLABLE check that the installed codex exposes the exact
        continuation surface: ``--json`` event output AND an ``exec resume``
        subcommand. Only ``--help`` is spawned (no model turn, no network), and
        the answer is memoized per resolved binary path for this process. An
        opted-in provider whose binary fails the probe runs FRESH and reports
        ``capability="unsupported"`` rather than silently attempting a resume the
        binary can't honor."""
        path = shutil.which("codex")
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
            # `codex exec --help` must advertise --json (the JSONL event stream we
            # parse for the exact thread id).
            help_res = run_reaped([path, "exec", "--help"], timeout=PROBE_TIMEOUT)
            if help_res is TIMEOUT:
                return False
            rc, out, err = help_res
            if rc != 0 or "--json" not in ((out or "") + (err or "")):
                return False
            # `codex exec resume --help` must exit 0: the exact-ID resume subcommand
            # this adapter drives has to exist. `--last` is deliberately NOT used.
            resume_res = run_reaped(
                [path, "exec", "resume", "--help"], timeout=PROBE_TIMEOUT
            )
            if resume_res is TIMEOUT:
                return False
            return resume_res[0] == 0
        except OSError:
            # A binary can disappear between which() and spawn. Treat that as an
            # unsupported capability so the provider can make a fresh attempt.
            return False

    @staticmethod
    def _first_thread_id(stdout: str) -> str | None:
        """Return the FIRST valid ``thread.started`` thread_id in a JSONL stream,
        else None. Defensive by contract: a malformed line, a non-object line, an
        unknown event type, or an id-less/duplicate event is a diagnostic, never a
        traceback; only the first well-formed event with a non-empty string
        thread_id wins."""
        for line in (stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(event, dict) or event.get("type") != "thread.started":
                continue
            tid = event.get("thread_id")
            if _valid_resume_id(tid):
                return tid
        return None

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
        start = time.monotonic()

        # Precedence: explicit model (CLI --model) > the seat's resolved model
        # (the catalog already folded the config layers over the shipped pin).
        # Unlike cursor, a None result is not an error: codex falls back to its
        # own CLI default when --model is omitted.
        chosen_model = model or self._default_model

        # --ignore-user-config: hermetic review seat — skip the user's
        # ~/.codex/config.toml (MCP servers, skills, RTK, AGENTS.md preamble).
        # Cuts ~40k startup tokens and keeps the seat reproducible. Because that
        # also drops the config's model_reasoning_effort, we RE-PIN it in code
        # via -c so reviews keep deep reasoning without the user-config bloat.
        # reasoning_effort: the seat's own tune (the catalog resolved the config
        # layers), else the built-in default.
        effort = self._reasoning_effort or DEFAULT_REASONING_EFFORT

        # Continuation is workspace-write-only and additive: read-only review and
        # unchained write keep their exact argv and six-field result. `capable`
        # is the cached RUNTIME probe, not supports_continuation; an opted-in
        # provider whose installed binary lacks the surface runs FRESH and reports
        # capability="unsupported". A continuation passed to a read-only call is
        # defensively ignored so a misuse can't perturb the review argv.
        want_continuation = continuation is not None and sandbox == "workspace-write"
        requested_resume_id = continuation.conversation_id if want_continuation else None
        if requested_resume_id is not None and not _valid_resume_id(requested_resume_id):
            return ProviderResult(
                name=self.name,
                model=chosen_model,
                ok=False,
                output="",
                error="codex resume ID is invalid",
                elapsed=time.monotonic() - start,
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

        # Shared exec options, order-stable. Assembled once, then framed either as
        # a fresh `exec - <options>` or a resume `exec <options> resume <id> -`.
        options: list[str] = []
        # --ignore-user-config makes a READ-ONLY review seat hermetic (skips
        # ~/.codex/config.toml + AGENTS.md + project conventions/skills, ~40k
        # startup tokens, reproducible). A WORK seat (/crew:dispatch,
        # sandbox=="workspace-write") WANTS that project context so its edits
        # match house style, so we OMIT the flag in write mode (RATIFIED
        # decision). The -c model_reasoning_effort pin stays UNCONDITIONAL so
        # dispatch keeps a deterministic effort even with user config loaded
        # (explicit -c wins). Read-only review/debate is byte-for-byte unchanged.
        if sandbox != "workspace-write":
            options += ["--ignore-user-config"]
        options += ["-c", f"model_reasoning_effort={effort}", "--sandbox", sandbox]
        # Dispatch options apply in WRITE MODE ONLY (the structural gate that
        # keeps every read-only review argv byte-identical even when a
        # [dispatch.codex] table is configured). --profile is write-mode
        # coherent: write mode loads the user config (--ignore-user-config
        # omitted above), which is the base the profile file layers onto, and
        # the explicit -c model_reasoning_effort pin still wins over
        # profile-supplied config (codex precedence: -c > profile > base).
        # Values arrive pre-validated from the config getter.
        if sandbox == "workspace-write" and dispatch_options:
            profile = dispatch_options.get("profile")
            if profile:
                options += ["--profile", profile]
        if chosen_model:
            options += ["--model", chosen_model]
        options += ["--skip-git-repo-check"]
        # --json turns stdout into a JSONL event stream we parse for the exact
        # thread id. It rides ONLY on a capable continuation run, so every other
        # path keeps byte-identical argv; the clean final message still comes from
        # -o, so events never leak into ProviderResult.output.
        if capable:
            options += ["--json"]
        # The -o temp file receives the clean final message. Capability probing
        # happens before this allocation so a failed probe cannot leak it.
        fd, out_path = tempfile.mkstemp(prefix="crew_codex_", suffix=".txt")
        os.close(fd)
        options += ["-o", out_path]

        if resume_id:
            # Resume: parent exec options first, then `resume <exact-id> -` with
            # the prompt still on stdin (the trailing `-`). Never `--last`.
            argv = ["codex", "exec"] + options + ["resume", resume_id, "-"]
        else:
            # Fresh (chained or not): the `-` stdin positional leads, exactly as
            # the unchained/read-only argv always has.
            argv = ["codex", "exec", "-"] + options

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

        def make_result(
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

        try:
            # Shared reaped runner: start_new_session + SIGTERM→SIGKILL
            # killpg teardown on timeout so a hung codex can't orphan billable
            # grandchildren. Prompt via stdin (input_text), cwd preserved.
            result = run_reaped(argv, input_text=prompt, timeout=timeout, cwd=run_cwd)
            if result is TIMEOUT:
                return make_result(
                    ok=False,
                    output="",
                    error=f"codex timed out after {timeout}s",
                    failure="timeout",
                )
            returncode, proc_stdout, proc_stderr = result
            captured_id = self._first_thread_id(proc_stdout) if capable else None

            if returncode != 0:
                if capable:
                    err = (proc_stderr or "").strip()
                else:
                    err = (proc_stderr or proc_stdout or "").strip()
                return make_result(
                    ok=False,
                    output="",
                    error=err or f"codex exited with status {returncode}",
                    failure="error",
                    conversation_id=captured_id,
                )

            # Read the clean final message from the -o file.
            output = ""
            try:
                with open(out_path, "r", encoding="utf-8", errors="replace") as f:
                    output = f.read().strip()
            except OSError:
                if not capable:
                    output = (proc_stdout or "").strip()

            if not output and not capable:
                # Fall back to stdout, else report empty as a failure.
                output = (proc_stdout or "").strip()
            if not output:
                return make_result(
                    ok=False,
                    output="",
                    error="codex returned no output",
                    failure="error",
                    conversation_id=captured_id,
                )

            failure = "none"
            if capable and phase == "resume" and captured_id != resume_id:
                failure = "error"
            persisted_id = captured_id if failure == "none" else None
            return make_result(
                ok=True,
                output=output,
                error=None,
                failure=failure,
                conversation_id=captured_id,
                continuation_id=persisted_id,
            )
        finally:
            try:
                os.unlink(out_path)
            except OSError:
                pass
