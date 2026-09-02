# crew

Persistence, specialized agents, and workflow commands for Claude Code.

## What's In Here

Standalone `/crew:review` uses the Python-owned workflow engine through the
Claude Code and Cursor host adapters. Cursor now issues its cursor-channel
seats as native subagents (`agents-cursor/` ships the three role adapters) only
when a seat declares a `native_model`; the shipped composer seat uses
`composer-2.5-fast`, while a seat without one is warned about and dropped. The
app's subagent surface must offer the exact native variant, and an unoffered
slug is usually refused at spawn and settles failed, with no CLI fallback. The
run record also reports whether the answering model was runtime-reported or
only requested.
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
