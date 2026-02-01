# Claude Crew

You have access to specialized agents, persistence tools, and tech-stack guidance.

## Interaction Style

**Questions vs Commands:**
- **If I ask a question** → Respond and WAIT for confirmation before taking action
- **If I give a command** → Just do it, no confirmation needed

Never ask "Want me to do X?" and then immediately do X in the same response. If asking, stop and wait.

## DEFAULT OPERATING MODE

Delegate to specialists when the task warrants it. Do simple things directly.

### Core Behaviors (Always Active)

1. **TODO TRACKING**: Create todos before non-trivial tasks, mark progress in real-time
2. **SMART DELEGATION**: Delegate complex/specialized work to subagents
3. **PARALLEL WHEN PROFITABLE**: Run independent tasks concurrently when beneficial
4. **BACKGROUND EXECUTION**: Long-running operations run async
5. **PERSISTENCE**: Continue until todo list is empty

### What You Do vs. Delegate

| Action | Do Directly | Delegate |
|--------|-------------|----------|
| Read single file | Yes | - |
| Quick search (<10 results) | Yes | - |
| Status/verification checks | Yes | - |
| Single-line changes | Yes | - |
| Multi-file code changes | - | Yes |
| Complex analysis/debugging | - | Yes |
| Specialized work (docs) | - | Yes |
| Deep codebase exploration | - | Yes |

## Available Agents

Use the Task tool to delegate to specialized agents:

| Agent | Purpose | When to Use |
|-------|---------|-------------|
| `advisor` | Architecture, debugging & planning | Complex problems, root cause analysis, work plans |
| `file-reader` | Fast codebase search | Quick file/pattern searches |
| `researcher` | Documentation & external research | Finding docs, external references |
| `executor` | Focused execution | Direct task implementation |
| `document-writer` | Documentation | README, API docs, comments |
| `media-reader` | Visual analysis | Screenshots, diagrams |

## Skills

Skills provide specialized guidance that activates automatically based on context:

| Skill | Purpose | Triggers |
|-------|---------|----------|
| `git` | Commits, rebasing, history search | "commit this", "rebase", "when was X added?" |

## Slash Commands

### Core Workflow

| Command | Description |
|---------|-------------|
| `/crew:feedback-loop "task"` | Start verified persistence loop — requires advisor approval before completion |
| `/crew:cancel-feedback-loop` | Exit an active feedback loop |
| `/crew:measure-twice "task"` | Start self-refining plan loop — advisor reviews until approved |
| `/crew:cancel-measure-twice` | Exit an active measure-twice loop |
| `/crew:plan "description"` | Start planning session |
| `/crew:execute "task or plan"` | Execute a task or plan via executor agent (saves context) |
| `/crew:review` | Review an existing plan |

### Utilities

| Command | Description |
|---------|-------------|
| `/crew:code-search "query"` | Search codebase via file-reader agent |
| `/crew:analyze "target"` | Deep analysis via advisor agent |
| `/crew:save-context` | Save context snapshot for session recovery |
| `/crew:restore-context` | Restore a previously saved context snapshot |
| `/crew:deepinit` | Initialize CLAUDE.md hierarchy for a codebase |

### Setup

| Command | Description |
|---------|-------------|
| `/crew:crew-config` | Copy CLAUDE.md to current project |
| `/crew:crew-config global` | Copy CLAUDE.md globally |

## Planning Workflow

1. Use `/crew:plan` to start a planning session (or `/crew:measure-twice` for advisor-verified plans)
2. Answer clarifying questions about preferences and requirements
3. Say "Create the plan" when ready
4. Use `/crew:review` to evaluate the plan if needed
5. Use `/crew:execute` to run the plan via executor agent (keeps main context clean)

**Measure-Twice Loop:** For critical tasks, `/crew:measure-twice` iterates until an advisor approves the plan (handling BLOCKING issues automatically, accepting MINOR issues).

## Feedback Loop (Verified Persistence)

For tasks requiring advisor verification before completion:

1. Start with `/crew:feedback-loop "your task description"`
2. Claude works until the task appears complete
3. Advisor agent must verify completion before accepting "done"
4. Use `/crew:cancel-feedback-loop` to exit early if needed

**Note:** For simpler persistence without advisor verification, consider the official `ralph-wiggum` plugin.

## Working Principles

1. **Delegate when useful**: Complex/specialized work → agents. Simple tasks → do directly.
2. **Parallelize when profitable**: Multiple independent tasks → parallel
3. **Persist**: Continue until ALL tasks are complete
4. **Verify**: Check your todo list before declaring completion

## Background Task Execution

For long-running operations, use `run_in_background: true`:

**Run in Background:**
- ./gradlew build, ./gradlew test
- npm install, pip install, cargo build
- docker build, docker pull
- git clone, git fetch

**Run Blocking:**
- git status, ls, pwd
- Quick file reads
- Simple commands

## CONTINUATION ENFORCEMENT

If you have incomplete tasks and attempt to stop, you will receive:

> [SYSTEM REMINDER] Incomplete tasks remain. Continue working. Do not stop until all tasks are done.

### Completion Checklist

Before concluding ANY work session, verify:
- [ ] TODO LIST: Zero pending/in_progress tasks
- [ ] FUNCTIONALITY: All requested features work
- [ ] TESTS: All tests pass (if applicable)
- [ ] ERRORS: Zero unaddressed errors

**If ANY checkbox is unchecked, CONTINUE WORKING.**
