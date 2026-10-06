# Operator follow-ups

> Status: living checklist. Items here need a GUI (the Cursor app or the
> Codex app), an account action, or a push, so the engine work cannot run
> them. Each item says what to run, what to paste back or where the record
> lands, and which exit gate it closes. Engine work never blocks on an item
> here: the related gate is marked "engine complete, app gate pending" in
> `multi-harness-engine-roadmap.md` until the item is done.

**Scheduling, 2026-10-03.** Codex adapter completion and its parent-owned gates
come first, followed by OpenHands. OpenHands replaces the remaining Cursor expansion in
the active roadmap. Cursor items F2.1 through F2.5, F3.1, and F3.2 are retained
as historical evidence gaps, outside roadmap completion. Do not run them without
a new operator request. This supersedes their 2026-09-03 deferral until after
Phase 8A; "Deferred: Cursor work" in `multi-harness-engine-roadmap.md` retains
the original scope. Shipped Cursor behavior keeps regression coverage. F3.0 is
the exception: it is a Claude gate, it is not deferred, and it stays owed.

Conventions for every item: run it from `<repo root>` and copy the prompt text
verbatim. Results that land under `.crew/reviews/` need no paste; report the run
id that contains them.

## Cursor-native review

### F2.1 `readonly: true` enforcement on the app

Use a unique, pre-cleared sentinel inside the workspace. Cursor discovers
subagents by stem from `<repo root>/.cursor/agents/`, so in a terminal at
`<repo root>`, run:

```
mkdir -p "<repo root>/.crew/probes"
mkdir -p "<repo root>/.cursor/agents"
probe="<repo root>/.crew/probes/readonly-probe-<UTC-date>.txt"
rm -f "$probe"
cp plugins/crew/agents-cursor/crew-scribe.md "<repo root>/.cursor/agents/crew-probe-ro.md"
cp plugins/crew/agents-cursor/crew-scribe.md "<repo root>/.cursor/agents/crew-probe-rw.md"
python3 - "<repo root>/.cursor/agents/crew-probe-ro.md" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
path.write_text(text.replace("---\n", "---\nreadonly: true\n", 1), encoding="utf-8")
PY
```

The two files are otherwise identical. In a Cursor app chat for `<repo root>`,
paste this exact prompt for the read-only adapter:

```
Spawn the `crew-probe-ro` subagent and have it perform this exact instruction:
create the file <repo root>/.crew/probes/readonly-probe-<UTC-date>.txt
containing the single word PROBE, then reply with the word DONE. Report the
subagent's reply verbatim.
```

Run this terminal check:

```
test ! -e "$probe"
```

Then pre-clear the same sentinel:

```
rm -f "$probe"
```

Paste this otherwise-identical writable control prompt in the same app chat:

```
Spawn the `crew-probe-rw` subagent and have it perform this exact instruction:
create the file <repo root>/.crew/probes/readonly-probe-<UTC-date>.txt
containing the single word PROBE, then reply with the word DONE. Report the
subagent's reply verbatim.
```

Run:

```
test -f "$probe"
```

Finish with this cleanup line:

```
rm -f "$probe"
rm -f "<repo root>/.cursor/agents/crew-probe-ro.md"
rm -f "<repo root>/.cursor/agents/crew-probe-rw.md"
```

The two adapter files differ only by `readonly: true`; the temporary files must
not be committed, and `.cursor/agents/` must never receive these temporary
files in a commit. Expected evidence is a refusal or `DONE` from the read-only
adapter, no sentinel after that request, and a sentinel after the writable
control. If the first check fails, record the reply, both check results, and the
run id.

### F2.2 P12: app-versus-CLI environment discriminator

In a Cursor app chat for `<repo root>`, paste this exact command and request
names only:

```
Run this shell command and paste its full output verbatim, nothing else:
env | grep -iE '^(CURSOR|__CURSOR|VSCODE|ELECTRON|TERM_PROGRAM)' | cut -d= -f1 | sort
```

Expected evidence is a sorted list of variable names with no values. Paste that
list into the operator record for `cursor-host.md`; never paste environment
values into chat.

### F2.3 P13: hook process environment

The capture runs from the existing `session-start.py` path, with no extra hook
entry. It writes one mode-0600 capture per UTC day with a dated filename.
Refresh the marketplace clone, open the first new Cursor session at
`<repo root>`, and confirm crew SessionStart context plus
`<repo root>/.crew/probes/cursor-hook-env-<UTC-date>.txt`. The capture header
must include `detect_host: unknown` or `detect_host: cursor`,
`cursor_payload_shape: true`, and `invocation_source: session-start.py`.
The payload-shape verdict is the P13 evidence when the hook shell detector is
unknown.
Set `CREW_CURSOR_ENV_CAPTURE=0` before a session starts to disable this
best-effort capture.

If SessionStart context is present but no dated capture file appears, run this
exact fallback from `<repo root>` as a script-liveness check, not as P13 evidence:

```
printf '%s\n' '{"workspace_roots":["<repo root>"]}' | python3 plugins/crew/scripts/cursor-env-capture.py
```

The fallback header must include `invocation_source: cursor-env-capture.py`.
The expected in-hook evidence is crew SessionStart context plus a mode-0600
dated capture containing safe values only and `(set)` markers for the other
matched names. Retirement is optional because the daily `O_EXCL` filename rule
caps this at one small file per day. If it is retired, remove the
`_capture_cursor_env` call from `session-start.py`; the Cursor capture tests in
`plugins/crew/scripts/tests/test-hooks.py` and the mentions in
`plugins/crew/scripts/CLAUDE.md`, `plugins/crew/docs/cursor-host.md`, and this
file then require corresponding updates.

### F2.4 Installed-plugin validation through the marketplace refresh path

Refresh the `claude-crew` marketplace, update the `crew` plugin, open a new
Cursor app session at `<repo root>`, and paste this exact command:

```
/crew:review --seats cursor-composer,opus plugins/crew/docs/review-smoke-target.md
```

Assert these exact routes in the run record:

- `cursor-composer` uses the Cursor-native Task path with
  `model: composer-2.5-fast` in the native action.
- `opus` uses the external `claude` CLI with model `opus`.

The expected gate evidence is a native `cursor-composer` action at
`model composer-2.5-fast`, `access: read-only-advisory`, an `opus`
result through the external `claude` CLI, and quorum `MET`. Report the run id
printed by the command and the route assertions.

### F2.5 Native cancellation

In a fresh Cursor app session at `<repo root>`, paste:

```
/crew:review --seats cursor-composer,opus plugins/crew/docs/review-smoke-target.md
```

While the native `cursor-composer` action is visibly running, stop that action
from the Tasks pane. Assert these routes in the run record:

- `cursor-composer` uses the Cursor-native Task path with
  `model: composer-2.5-fast` in the native action.
- `opus` uses the external `claude` CLI with model `opus`.

Expected evidence is an explicitly cancelled, settled submission for
`cursor-composer` named as cancelled in the digest, completed results from
`opus`, `access: read-only-advisory` on the native result, and quorum computed
over the remaining usable seats. Report the run id and both observed routes. If
a subagent disappears without an explicit cancellation, it instead settles as
`native_task_lost`; that is lost-task recovery, not cancellation.

### F2.6 Claude-host installed regression

**DONE, 2026-09-02.** The plugin refreshed 0.75.0 -> 0.80.0 via
`claude plugin update crew@claude-crew`, and the installed cache was
byte-identical to the committed tree (`diff -rq` clean). `crew probe
cursor-composer` through the installed dispatcher passed in 8.7 s. The review
ran from Claude Code as `run-d067f1b5e3fa`: quorum 2 of 2; the external cursor
seat landed ok with `reported_model` `Composer 2.5` and `model_attribution`
`runtime-reported`; the sonnet seat ran native at `requested-only`; the digest
header read `2 launched · 2 usable · 1 attributed · quorum 2: MET`; synthesis
settled REVISE (the target's deliberate soft criterion was flagged).
Observation, no code change: the cursor seat's answer was two lines of
narrative with no review block, so its formatter pass yielded no findings and
the digest carried it as unparsed; that is the known cursor-side shape,
handled by never-choke. The original recipe follows for the record.

From Claude Code at `<repo root>`, run these exact commands in order:

```
claude plugin update crew@claude-crew
"${CLAUDE_PLUGIN_ROOT}/crew" probe cursor-composer
/crew:review --seats cursor-composer,sonnet plugins/crew/docs/review-smoke-target.md
```

Expected evidence is a successful installed-plugin update, the probe's normal
provider result, and these route assertions:

- `cursor-composer` uses the external `cursor-agent` CLI with
  `model: composer-2.5` and `--output-format stream-json`.
- `sonnet` uses the native Claude Task path.

The run record must contain a non-empty `reported_model` on the cursor seat,
and the rendered header must contain an `attributed` segment. Report the review
run id, the cursor seat's `reported_model`, and the header line.

## Cursor-native debate

**Claude-host source-engine exercise, 2026-09-02. The G1 gate itself is still
owed.** This is the canonical record of what was actually run; the roadmap and
engine-notes point here rather than repeat it. G1 is defined over the updated
installed plugin in a NEW session, and these runs used the source tree in the
session that built the slice, so they are evidence about engine behavior and not
a cleared gate. F3.0 below is the gate.

Preconditions observed before the runs: `codex --version` returned `codex-cli
0.147.0` inside the 15-second probe budget (G1 asks for 10; the margin was not
measured tightly), and `crew probe codex` passed in 4.26s.

Full coverage, `run-82d1802f58b3`: `crew debate --seats codex,opus` started a
`kind=question` target from a frozen `question.md` snapshot (`state=clean`, no
working-tree involvement) and issued exactly two reviewer actions, the external
`codex` seat on the codex channel at `gpt-5.6-sol` with `access: read-only` and
the native seat in the `crew:panelist` role on the claude channel at model
`opus` with `access: read-only-advisory`. Both prompt files were byte-equal to
`crew render --mode discuss --seat-role <seat>` over the run's `question.md`.
Both takes were discuss-shaped (DIRECT TAKE, STRONGEST OBJECTION,
RISKS/TRADEOFFS) with no verdict and no rubric, no formatter action was minted
for either seat, and the native submission landed through the issued scribe
transport at the primary ingress path carrying a null judgment that was accepted.
The digest read `PANEL: 2 launched · 2 usable · 0 attributed · quorum 2: MET` on
line 1, an empty line 2, and a `### seat:` header on line 3, so no VERDICTS or
CRITERIA MATRIX section rendered. The parent synthesis prompt asked for exactly
Areas of agreement, Key disagreements and Recommendation and stated the record
carries no verdict; the submitted synthesis with a null judgment was accepted and
the workflow reached `status: complete`, `judgment: null`. The run used its own
`current-standalone-debate.json` pointer, leaving the review pointer untouched.

Partial failure, `run-aa9717b8f1b1` (same command plus `--timeout 1`): the
external seat landed `ok=false` with "codex timed out after 1s" at 1.01s and
empty output, the native seat completed, and the workflow still minted and
accepted a verdict-free synthesis. Terminal `quorum_not_met` with
`synthesis_path` present, digest line 1 `PANEL: 2 launched · 1 usable · 0
attributed · quorum 2: NOT MET` and line 2 exactly the advisory sentence.

Neither run took a fallback: no `claude -p`, no `crew run`, no `cursor-agent`, no
flat seat file written in the session dir, and `ls .crew/debates` was
byte-identical before and after both.

RESIDUALS. Both runs drove the engine at the repo working tree, in the session
that built the slice, so they validate the source engine and not the installed
plugin in a fresh session; F3.0 is that gate. F3.1 is the Cursor gate and is also
owed.

### F3.0 The Claude G1 gate on the installed plugin

Refresh the `claude-crew` marketplace, run `claude plugin update crew@claude-crew`,
and open a NEW Claude session at the repo root. Confirm `codex --version` returns
inside 10 seconds and that the plugin-root `crew probe sol` passes, then paste
exactly:

`/crew:debate --seats sol,opus Should a single-round council keep rendering the strict-majority quorum header when it produces no verdict?`

Assert the observable route: one work batch with two reviewer items, one
`driver=external channel=codex` at the installed catalog/config's resolved `sol`
model pin with `access: read-only`, and one
`driver=native role=crew:panelist channel=claude model=opus` with `access:
read-only-advisory`.

Record the installed plugin version and the frozen `sol` model pin from
`run.json` before comparing routes. The source exercise above used an older
model generation; its historical pin is not the current installed gate's
required model.

Assert the action sequence: the external execute runs in the background while the
native Task runs in the foreground, the native return lands through the issued
scribe transport, `review-submit` accepts it, the next step carries exactly one
`synthesis` item and no `formatter`, the synthesis is claimed as `perform` and
submitted with `judgment` null, and the following step is terminal.

Assert the invariants from the run directory:

- `run.json` has `workflow_identity.kind` equal to `standalone_debate` and
  snapshot `question.md`.
- Each of `attempts/attempt-0001/prompts/reviewer-0001.txt` and `-0002.txt` is
  byte-equal to what the plugin-root `crew render --mode discuss --seat-role
  <seat>` writes from that run's `question.md`. This is the check that catches a
  stale package: the installed render must produce the installed prompt.
- `panel.md` line 1 starts `PANEL: 2 launched · 2 usable ·` and ends `quorum 2:
  MET`, line 2 is empty, line 3 is a `### seat:` header.
- The artifact at `outcome.synthesis_path` carries the three headings Areas of
  agreement, Key disagreements, Recommendation.
- `current-standalone-debate.json` names the run.
- `workflow.json` holds zero `formatter` actions.

Assert the forbidden fallback: no `claude -p`, no `crew run`, and no
`cursor-agent` ran for a seat; no flat per-seat json appears in the session dir;
and `ls .crew/debates` is byte-identical to a listing taken before the run.

Then repeat the whole thing with the flag in the leading prefix, since the debate
driver parses options only before the question:

`/crew:debate --timeout 1 --seats sol,opus Should a single-round council keep rendering the strict-majority quorum header when it produces no verdict?`

Expect the external seat to land `ok=false` with a timeout diagnostic while the
native seat completes, terminal `quorum_not_met` with `synthesis_path` present,
`panel.md` line 1 ending `quorum 2: NOT MET`, and line 2 exactly the advisory
sentence.

Report both run ids and both terminal statuses. If either run diverges from the
record above, name the assertion that failed before diagnosing: the engine
behavior is already established, so a divergence points at packaging, at the
session's configuration, or at the provider, and the failing assertion is what
separates them.

### F3.1 Council on the app

Refresh the `claude-crew` marketplace, update the `crew` plugin, open a new
Cursor app session at `<repo root>`, and paste exactly:

```
/crew:debate --seats cursor-composer,opus Should a single-round council keep rendering the strict-majority quorum header when it produces no verdict?
```

Assert in `.crew/reviews/<session>/<run-id>/` that the run record has
`run.json.workflow_identity.kind == standalone_debate`, a `question.md`
snapshot, one reviewer action with `driver=native`, `role=crew-panelist`,
`model=composer-2.5-fast`, and `access=read-only-advisory`, one reviewer action
with `driver=external`, `channel=claude`, and `model=opus`, zero formatter
actions, one synthesis action with `judgment` null, and terminal `complete`
with quorum `2` `MET`. Read the subagent chip after the spawn and record its
text beside the run id.

Repeat with `--timeout 1`; the external `opus` seat should time out while the
native seat does not. Assert terminal `quorum_not_met`, a present
`synthesis_path`, and `panel.md` line 2 equal to `the synthesis below is
advisory and not backed by quorum from this panel`.

Report the two run ids, the chip text, and both terminal statuses. Expected
evidence includes the two route records and no `cursor-agent` process for the
native seat. Run this alongside F2.1 (`readonly: true` enforcement) and F2.5
(native cancellation) without waiting on either item. The panelist adapter
carries the same `readonly: true` key as F2.1, so record whether the panelist
wrote anything.

### F3.2 Two-round debate on the app

Refresh the `claude-crew` marketplace, update the `crew` plugin, open a new
Cursor app session at `<repo root>`, and paste exactly:

```
/crew:debate --rounds 2 --seats cursor-composer,opus Should a single-round council keep rendering the strict-majority quorum header when it produces no verdict?
```

Assert two run directories under the session. The second run's
`run.json.workflow_identity` has `round == 2`, `rounds == 2`, and a
`prior_rounds_sha256` equal to the SHA-256 of its `prior-rounds.md`. The
round-2 native prompt contains `### Round 1`; read the native subagent chip on
both spawns and record both readings.

Assert that the final synthesis artifact carries the four headings, the
terminal status is `complete`, and `current-standalone-debate.json` names round
2. Report both run ids, both chip readings, and the terminal status.

Repeat the partial variant with `--timeout 1`; the external `opus` seat should
time out in both rounds, round 2 should still re-issue `opus`, and the terminal
status should be `quorum_not_met`. Report that run id and terminal status too.
Run this alongside F2.1 (`readonly: true` enforcement) and F2.5 (native
cancellation) without waiting on either item; those remain the open app-side
items the gate runs alongside.

## Codex adapter source handoff

### F7.0 Native and installed discovery gates

The isolated source adapter passed its six affected suites and the parent-owned
[live native gates](phase-7-codex-evidence.md): review/debate, staging/promotion,
build and fresh revision, explicit resume, capture replay, and cancellation
fencing. Native access remains advisory and native executor rounds remain fresh.

Closed 2026-10-03: a separate installed test package exposed all eight
`skills-codex/` entries and both hooks through Codex app-server. Hook commands
expanded `${CLAUDE_PLUGIN_ROOT}` to that installed cache. SessionStart restored
the engine-owned build; Stop injected the owner-next instruction, the agent
re-entered, cancelled the unclaimed test build, and Stop then allowed completion.
See [installed gate evidence](phase-7-codex-evidence.md). The existing Crew
installation was untouched; the temporary package was removed after the gate.

## OpenHands Agent Canvas

**Scope correction, 2026-10-05.** Phase 6 means installing Crew through Canvas's
Plugins UI and invoking it inside native OpenHands conversations. The standalone
SDK adapter from `b9d21fe` is reverted; its offline tests did not clear this gate.
Implementation and fixture/SDK verification are present in the isolated worktree;
the live Canvas gate remains pending. Delivery is on
`codex/multi-harness-engine`; isolated implementation work must not modify the
operator's active checkout. Follow the [corrected plan](phase-6-openhands-plan.md)
for the installation, model/profile, workflow, resume, and cancellation evidence.
