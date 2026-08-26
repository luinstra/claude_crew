# crew

Persistence, specialized agents, and workflow commands for Claude Code.

## What's In Here

Standalone `/crew:review` uses the Python-owned workflow engine through the
Claude Code and Cursor host adapters. Cursor now issues its cursor-channel
seats as native subagents (`agents-cursor/` ships the three role adapters);
a cursor seat pinned to a run-time alias such as `auto` is warned about and
dropped from the panel, and one whose exact model slug the app's subagent
surface does not offer (the family must be enabled in Cursor AND the pin must be
the variant the Task path accepts, such as `composer-2.5-fast`) is usually
refused at spawn and settles failed, with no CLI fallback; the same slug has also been
accepted with its answering model unobserved, so substitution is possible and a
native seat's answering model is verified only by its badge in the app.
Codex-host all-external protocol compatibility is covered
deterministically through the Python CLI, but the Codex plugin does not yet
expose standalone `/crew:review`; its app-native adapter remains deferred.
Build and measure-twice continue to use `review-prep`.

- **8 specialized agents** — advisor, executor, reader, document-writer, reviewer, panelist, formatter, scribe
- **Slash commands** — planning, execution, search, build/measure-twice loops
- **2 lifecycle hooks** — SessionStart context restoration, Stop persistence enforcement
- **Python state machine** — session-scoped JSON state files in `.crew/`

## Install

From the repo root:

```
/plugin
```

(Run from `plugins/crew/` to install just this plugin.)

## Usage

See the [project README](../../README.md) and [user guide](../../docs/CLAUDE.md) for command reference and examples.

## Development

See [`.claude/CLAUDE.md`](../../.claude/CLAUDE.md) for the development guide, [`scripts/CLAUDE.md`](./scripts/CLAUDE.md) for the state machine internals, and [`docs/engine-notes.md`](./docs/engine-notes.md) for the rationale/history behind the engine contracts.
