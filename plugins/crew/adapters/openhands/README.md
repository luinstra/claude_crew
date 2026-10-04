# Native OpenHands adapter

Optional local adapter for Crew's review, debate, measure-twice and build engines.
It uses native SDK agents and asynchronous conversations. Crew still owns action
claims, review policy, human decisions, budgets, result capture and writer fences.
This is an initial file-tool implementation with offline verification; the live
provider gate has not run. It is not yet full executor parity.

The separate uv environment pins `openhands-sdk==1.51.0` and Python 3.12+.
Crew's Python 3.11 stdlib core imports no SDK package. Existing seats and CLI routes
retain their meaning. No OpenHands seat or credential is selected by default.

## Setup and explicit routes

Run `uv sync --frozen` in this directory. Add explicit seats to the workspace's
`.crew/config.toml`, substituting actual supported provider/model IDs:

```toml
[seats.oh-reviewer]
via = ["openhands"]
model = "provider/model"

[openhands]
advisor_model = "provider/model"
executor_model = "provider/model"
```

Keep SDK connection settings in a separate `runtime.toml`. Secrets are referenced
by environment variable name, not copied from CLI authentication or written here:

```toml
max_concurrency = 2
max_iterations = 50
support_model = "provider/model"

[models."provider/model"]
api_key_env = "MY_PROVIDER_API_KEY"
# base_url = "https://your-explicit-gateway.example/v1"
# max_input_tokens = 32768
# max_output_tokens = 4096
```

Every native seat, advisor, executor and support model needs an exact matching SDK
route. No model fallback is enabled. `support_model` runs substantive formatter
or synthesis actions issued by Crew; no model performs dispatch bookkeeping.
Use a second explicit seat and model route for a multi-model panel.

The dedicated runner selects `CREW_HOST=openhands` for its own process and sets
`OH_PERSISTENCE_DIR` before importing the SDK. It does not modify global settings.
From this directory:

```sh
uv run --frozen python runner.py --workspace /absolute/repo --config /absolute/runtime.toml --session-id my-session review /absolute/repo/plan.md --seats oh-reviewer
uv run --frozen python runner.py --workspace /absolute/repo --config /absolute/runtime.toml --session-id my-session debate "Choose the design" --seats oh-reviewer --rounds 2
uv run --frozen python runner.py --workspace /absolute/repo --config /absolute/runtime.toml --session-id my-session measure-twice --arguments="--seats oh-reviewer /absolute/repo/requirements.md"
uv run --frozen python runner.py --workspace /absolute/repo --config /absolute/runtime.toml --session-id my-session build --arguments="--seats oh-reviewer --executor crew:executor Implement the approved plan"
```

The runner returns Crew's structured step as JSON. A human question remains a
human question: use the existing Crew decision command with its exact question
ID, then explicitly resume. Save the returned `ref` object as `ref.json`:

```sh
uv run --frozen python runner.py --workspace /absolute/repo --config /absolute/runtime.toml --session-id my-session resume --kind build --ref /absolute/ref.json
```

Resume captures an already-settled SDK result into its pending Crew claim without
another model call. A missing or uncertain receipt stops with the claim/fence
intact; use Crew's existing recovery after establishing what happened. It never
silently starts a replacement writer. Keep `.crew/openhands` private because SDK
conversation persistence may contain source and model responses.

## Enforced capabilities and current limits

Reviewers, panelists, formatters and synthesis agents have read/list tools only.
Advisors can write only their engine-issued plan staging file. Executors can write
workspace files and create directories, excluding `.git`, `.crew`, `.agents` and
`.codex`. Tools reject parent traversal, symlinks and multiply-linked write targets.
There are no shell, MCP, network or delegation tools. SDK ambient plugin/skill
loading is disabled, and the actual tool map is checked before execution.

Cancellation revokes file access and waits for any active file operation before
capturing a settled result. Merely receiving SDK interruption acknowledgement is
insufficient. On an uncertain failure, Crew's outstanding-writer fence remains.

Build revisions currently use fresh, independently bound executor conversations.
Cross-action executor continuation and sandboxed shell/test execution remain
follow-ups. An executor must report BLOCKED when verification requires unavailable
tools. Do not interpret the file-writing build test as general build/test support.
Remote Agent Server, product UI, automatic resume and provider subscription-login
integration are not implemented or claimed.

## Verification

```sh
uv run --frozen python -m unittest test_sdk_contract test_runtime test_bridge test_runner
```

These tests use actual SDK agents, conversations, tool dispatch, persistence and
Crew engine transitions. Provider responses are fixtures. Fixture model names
are examples, not operator-selected routes or evidence of live authentication.

See [implementation status](../../docs/phase-6-openhands-plan.md) and
[recorded evidence](../../docs/phase-6-openhands-evidence.md).
