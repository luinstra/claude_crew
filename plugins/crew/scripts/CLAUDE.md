# Scripts - Claude Code Guidance

> **Context:** Python state machine for persistence loops + the multi-model review engine
> **Parent Guide:** [Project Root](../../../.claude/CLAUDE.md)

## Quick Reference

This directory contains the Python backend for crew's persistence features. Hooks receive JSON on stdin, process state, and output JSON results. The `crew-state.py` CLI manages loop state files. The `multiagent/` package is the review/council engine that fans a prompt across subprocess seats.

## Structure

```
scripts/
├── crew-state.py        # CLI for loop state management
├── models.py            # Dataclasses for all JSON structures
├── persistent-mode.py   # Stop hook: enforces continuation
├── session-start.py     # SessionStart hook: restores state
├── test-hooks.py        # Unit tests (state machine + hooks)
├── test-multiagent.py   # Unit tests for the multiagent engine
└── multiagent/          # Multi-model review/council engine (see below)
```

## Multi-Model Review Engine (`multiagent/`)

The engine that powers `/crew:review`, `/crew:debate`, and the review steps of
`/crew:build` and `/crew:measure-twice`. It drives **subprocess seats** only —
the external CLIs `codex` and `agy`. The **Claude seats** (opus/sonnet) are NOT
in here: they're spawned by the orchestrating command as `crew:reviewer` Task
subagents (in-session, on the subscription), and the orchestrator normalizes
their results into the same six-field shape the engine returns.

```
multiagent/
├── cli.py               # argparse entry: `review` | `council` | `run` subcommands
├── prompts.py           # plan/code/council prompt builders (reference + inline modes)
├── targets.py           # resolve a plan .md or a git diff target (+ the diff_cmd/ref_path a seat reproduces)
├── render.py            # side-by-side panel + --json rendering
└── providers/
    ├── __init__.py      # ProviderResult (the six-field contract), Provider ABC, registry
    ├── codex.py         # CodexProvider — `codex exec - --sandbox read-only -o <tmp>`, prompt via stdin
    └── agy.py           # AgyProvider — `agy -p <prompt> --model … --sandbox` (NOT --dangerously-skip-permissions)
```

Key contracts (do NOT regress):
- **Six-field `ProviderResult`** (`name, model, ok, output, error, elapsed`) — the
  one shape every seat (subprocess AND normalized Task seat) returns.
- **Never choke** — a failed/skipped seat returns `ok=False`; the fan-out only
  exits nonzero when EVERY seat failed (and still emits the full array, no traceback).
- **Reference mode is the default** — seats fetch the diff/plan themselves (the
  engine passes `targets.Target.diff_cmd` / `ref_path`); `--inline-diff` embeds it.
  The Claude Task seats (`crew:reviewer`) have `Read, Grep, Glob, Bash` and fetch
  the target the same way — run the diff command, or read the plan path —
  Bash scoped to read-only inspection (git diff/show/log, reads); never mutate.
- **`--out <file>`** writes results to a file so the call needs no shell redirect
  (stays permission-allowlistable).
- Tuning env: `CREW_MA_SEATS`, `CREW_MA_TIMEOUT`, `CREW_MA_CODEX_MODEL`,
  `CREW_MA_AGY_MODEL`, `CREW_MA_AGY_PRINT_TIMEOUT`.

Run its tests with `python plugins/crew/scripts/test-multiagent.py`.

## Key Patterns

### Hook I/O Pattern

All hooks follow the same pattern:

```python
#!/usr/bin/env python3
import json
import sys
from models import StopInput, HookResult

def main():
    # 1. Read JSON from stdin
    data = json.load(sys.stdin)

    # 2. Parse into typed model
    input = StopInput.from_dict(data)

    # 3. Do logic
    if should_block(input):
        result = HookResult.block("Reason to continue working")
    else:
        result = HookResult.allow()

    # 4. Output JSON
    print(result.to_json())

if __name__ == "__main__":
    main()
```

### State File Pattern

State is stored as JSON in `.crew/`:

```python
from pathlib import Path
from models import BuildState

# Get project directory (from env or cwd)
directory = Path(os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd()))

# Load state (returns default if file missing)
state_file = directory / ".crew" / "build-state.json"
state = BuildState.load(state_file)

# Modify and save
state.iteration += 1
state.save(state_file)  # Creates parent dirs, sets 0o600 permissions
```

### SessionStart Output Pattern

SessionStart hooks use `additionalContext`, not `message`:

```python
from models import SessionStartResult

# Inject context into Claude's session
result = SessionStartResult.with_context("""
[Previous session context restored]
You were working on: fixing the auth bug
""")
print(result.to_json())
```

## crew-state.py CLI

All subcommands accept `--session-id SESSION_ID` for session-scoped state files.
If omitted, falls back to `CLAUDE_SESSION_ID` env var, then legacy unsuffixed filenames.

```bash
# Show loop state
crew-state.py show bl --session-id abc123
crew-state.py show mt

# Check if active (exit code 0=active, 1=inactive)
crew-state.py is-active bl --session-id abc123

# Initialize a loop
crew-state.py init bl --prompt "Fix the auth bug" --session-id abc123
crew-state.py init mt --task "Add user profiles" --auto-plan --session-id abc123

# Check if this session has conflicts (other sessions ignored)
crew-state.py check-conflicts --session-id abc123

# Set specific fields
crew-state.py set bl iteration 5 --session-id abc123
crew-state.py set mt last_verdict REVISE --session-id abc123

# Deactivate
crew-state.py deactivate bl --reason "User cancelled" --session-id abc123
```

**Session ID resolution order:**
1. `--session-id` CLI argument (highest priority)
2. `CLAUDE_SESSION_ID` environment variable
3. Empty string -- legacy unsuffixed filenames (backward compatible)

**Session-scoped filename pattern:**
- `build-state-{session_id}.json`
- `measure-twice-state-{session_id}.json`

**Loop aliases:**
- `bl` = `build`
- `mt` = `measure-twice`

## Data Models

### HookResult

```python
@dataclass
class HookResult:
    continue_: bool = True    # Allow or block
    message: Optional[str]    # Info message
    reason: Optional[str]     # Block reason

# Usage
HookResult.allow()                    # {"continue": true}
HookResult.allow("Info message")      # {"continue": true, "message": "..."}
HookResult.block("Must continue")     # {"continue": false, "reason": "..."}
```

### BuildState

```python
@dataclass
class BuildState:
    active: bool = False
    prompt: str = ""              # Original task
    iteration: int = 1
    max_iterations: int = 10
    completion_promise: str = "DONE"
    session_id: str = ""          # Session that owns this loop
```

### MeasureTwiceState

```python
@dataclass
class MeasureTwiceState:
    active: bool = False
    task_description: str = ""
    plan_file: str = ""           # e.g., ".crew/plans/auth-system.md"
    iteration: int = 1
    max_iterations: int = 10
    last_verdict: str = ""        # APPROVED, REVISE, REJECT
    session_id: str = ""          # Session that owns this loop
```

## Common Mistakes

❌ **Using `message` for SessionStart context**
```python
# WRONG - message is for PermissionRequest, not context injection
print(json.dumps({"message": "You were working on X"}))
```

✅ **Use `additionalContext` via SessionStartResult**
```python
result = SessionStartResult.with_context("You were working on X")
print(result.to_json())
```

---

❌ **Hardcoding project directory**
```python
state_file = Path("/Users/me/project/.crew/state.json")
```

✅ **Read from environment**
```python
directory = Path(os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd()))
state_file = directory / ".crew" / "state.json"
```

---

❌ **Not handling missing state files**
```python
with open(state_file) as f:
    state = json.load(f)  # Crashes if file missing
```

✅ **Use model's load() method**
```python
state = BuildState.load(state_file)  # Returns default if missing
```

## Orphan Cleanup

The `session-start.py` hook cleans up stale state files on every session start:

- **Inactive** state files older than **1 day**: deleted
- **Active** state files older than **7 days**: force-deleted (safety limit for abandoned sessions)
- Applies to `build-state-*` and `measure-twice-state-*` files

## Testing

```bash
# Run all tests
python test-hooks.py

# Run with verbose output
python test-hooks.py -v
```

Tests cover:
- Model serialization/deserialization
- State file operations
- Hook result formatting
- CLI commands (legacy and session-scoped)
- Multi-session isolation (concurrent loops, conflict detection)
- Orphan cleanup (stale inactive and very old active files)
- `CLAUDE_SESSION_ID` env var fallback

## Working Here Checklist

- [ ] Import models from `models.py`, don't duplicate dataclasses
- [ ] Use `CLAUDE_PROJECT_DIR` env var for directory
- [ ] Handle missing state files gracefully (return defaults)
- [ ] Set file permissions to 0o600 for state files
- [ ] Run `test-hooks.py` after changes
- [ ] Output valid JSON only (no print debugging to stdout)

## Related Guides

- [Project Root](../../../.claude/CLAUDE.md) - Adding hooks, overall architecture
