# Scripts - Claude Code Guidance

> **Context:** Python state machine for persistence loops and session lifecycle
> **Parent Guide:** [Project Root](../../../.claude/CLAUDE.md)

## Quick Reference

This directory contains the Python backend for crew's persistence features. Hooks receive JSON on stdin, process state, and output JSON results. The `crew-state.py` CLI manages loop state files.

## Structure

```
scripts/
├── crew-state.py       # CLI for loop state management
├── models.py           # Dataclasses for all JSON structures
├── persistent-mode.py  # Stop hook: enforces continuation
├── session-start.py    # SessionStart hook: restores state
└── test-hooks.py       # Unit tests
```

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
from models import FeedbackLoopState

# Get project directory (from env or cwd)
directory = Path(os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd()))

# Load state (returns default if file missing)
state_file = directory / ".crew" / "feedback-loop-state.json"
state = FeedbackLoopState.load(state_file)

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

```bash
# Show loop state
crew-state.py show fl          # feedback-loop
crew-state.py show mt          # measure-twice

# Check if active (exit code 0=active, 1=inactive)
crew-state.py is-active fl

# Initialize a loop
crew-state.py init fl --task "Fix the auth bug"
crew-state.py init mt --task "Add user profiles" --auto-plan

# Set specific fields
crew-state.py set fl iteration 5
crew-state.py set mt last_verdict REVISE

# Deactivate (deletes state file)
crew-state.py deactivate fl --reason "User cancelled"
```

**Loop aliases:**
- `fl` = `feedback-loop`
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

### FeedbackLoopState

```python
@dataclass
class FeedbackLoopState:
    active: bool = False
    prompt: str = ""              # Original task
    iteration: int = 1
    max_iterations: int = 20
    completion_promise: str = "DONE"
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
state = FeedbackLoopState.load(state_file)  # Returns default if missing
```

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
- CLI commands

## Working Here Checklist

- [ ] Import models from `models.py`, don't duplicate dataclasses
- [ ] Use `CLAUDE_PROJECT_DIR` env var for directory
- [ ] Handle missing state files gracefully (return defaults)
- [ ] Set file permissions to 0o600 for state files
- [ ] Run `test-hooks.py` after changes
- [ ] Output valid JSON only (no print debugging to stdout)

## Related Guides

- [Project Root](../../../.claude/CLAUDE.md) - Adding hooks, overall architecture
