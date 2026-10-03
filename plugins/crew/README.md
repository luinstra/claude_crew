# crew

Persistence, specialized agents, and workflow commands for Claude Code and Codex.

## What's In Here

Standalone `/crew:review` and `/crew:debate` use the Python-owned workflow
engine through the Claude Code and Cursor host adapters. Cursor now issues its
cursor-channel seats as native subagents (`agents-cursor/` ships four role
adapters, including `crew-panelist.md`) only
when a seat declares a `native_model`; the shipped composer seat uses
`composer-2.5-fast`, while a seat without one is warned about and dropped. The
app's subagent surface must offer the exact native variant, and an unoffered
slug is usually refused at spawn and settles failed, with no CLI fallback. The
run record also reports whether the answering model was runtime-reported or
only requested.
Codex has a source adapter and four workflow skills and four cancel/context helpers over the shared
protocols. Native review spends frozen model/effort with advisory inherited
access; explicit host-written capture retains the final reply without a scribe.
Live native gates passed on 2026-10-03; the installed discovery gate and
automatic hook re-entry also passed in a separate test package. See [codex-host.md](docs/codex-host.md).
Measure-twice and build admit native Codex advisor/built-in executor roles.
Native executor rounds are fresh; configured external write routes and their
exact-conversation continuation stay unchanged.

- **8 specialized agents** — advisor, executor, reader, document-writer, reviewer, panelist, formatter, scribe
- **Cursor role adapters** in `agents-cursor/`: reviewer, panelist, formatter, scribe.
- **Codex skills** in `skills-codex/`: four workflows plus four cancel/context helpers.
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

Measure-twice request grammar and supported host facts:
[protocol](docs/measure-twice-protocol.md). Real Claude Code CLI lifecycle cases,
including automatic Stop continuation and exact direct capture, have
[recorded evidence and limits](docs/phase-4-measure-twice-evidence.md).

Build uses the [engine-owned protocol](docs/build-protocol.md) with native Claude
execution, configured external write routes, explicit writer recovery and bound
human decisions. [Phase 5 evidence](docs/phase-5-build-evidence.md) records the
verified source/host epochs and their limits. Cursor native build remains deferred.
