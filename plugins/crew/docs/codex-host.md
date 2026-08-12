# Codex host contract

What crew has verified about running inside the Codex/ChatGPT harness.
Every fact carries an evidence tag: (verified: probe date S<n>/P<m>,
codex <ver>), (inferred: basis), or (unverified). Facts without a
verified tag are expectations, not guarantees.

## Detection and environment

- Codex supplies exact distinctive env names: CODEX_THREAD_ID, CODEX_CI, and CODEX_SANDBOX_NETWORK_DISABLED were present in two independent captures, and none appears in the recorded genuine Claude host env (verified: probe 2026-08-12 S1/P1 and S2/P1, codex 0.147.0)
- The marker table adopts CODEX_THREAD_ID and CODEX_SANDBOX_NETWORK_DISABLED; CODEX_CI is observed but not adopted because a CI-flavored name is the likeliest future collision (verified: probe 2026-08-12 S1/P1 and S2/P1, codex 0.147.0)
- Codex does NOT emulate Claude: CLAUDECODE and CLAUDE_CODE_ENTRYPOINT are absent, in both captures (verified: probe 2026-08-12 S1/P1 and S2/P1, codex 0.147.0)
- Unaided detection before the marker fill read unknown, the conservative fallback; with markers filled it reads codex, and the CREW_HOST=codex export remains an operator override, no longer mandatory (verified: probe 2026-08-12 S1/P0, codex 0.147.0)
- Prefix matching on CODEX is unusable as a detector: genuine Claude hosts carry CODEX_COMPANION_* companion vars, so only the exact adopted names count (inferred: recorded Claude-host capture)
- CLAUDE_PROJECT_DIR is unset in this host, so crew_base() resolves from the process cwd; run crew commands from the project root (verified: probe 2026-08-12 S1/P0, codex 0.147.0)
- The crew plugin installs via the codex plugin CLI to ~/.codex/plugins/cache/<marketplace>/<plugin>/<version>/crew, depth 6 from ~/.codex, so a locate walk needs -maxdepth 6 (verified: probe 2026-08-12 S1/P0, codex 0.147.0)

## Hook integration

- SessionStart and Stop both fire, delivered to the crew hook scripts registered in hooks.json (verified: probe 2026-08-12 S1/P2, codex 0.147.0)
- SessionStart payload keys: cwd, hook_event_name, model, permission_mode, session_id, source, transcript_path (verified: probe 2026-08-12 S1/P2, codex 0.147.0)
- Stop payload keys: cwd, hook_event_name, last_assistant_message, model, permission_mode, session_id, stop_hook_active, transcript_path, turn_id (verified: probe 2026-08-12 S1/P2, codex 0.147.0)
- The payload session id key is spelled session_id and arrives NON-empty, so crew state commands can take --session-id from the hook payload in this host (verified: probe 2026-08-12 S1/P2, codex 0.147.0)

## Loops

- The stop-block outcome is POSITIVE: three consecutive genuine stop attempts against an armed measure-twice loop each coerced continuation, with the crew Stop nudge injected verbatim as a hook_prompt, stop_fires advancing 0 to 3 on the armed state file, and the hook stamping the harness session id into the legacy unsuffixed state it adopted (verified: probe 2026-08-12 S1/P3, codex 0.147.0)
- Applied scope, per the recorded decision rule: loops are supported in this host as-is; no one-shot-only restriction ships (verified: probe 2026-08-12 S1/P3, codex 0.147.0)
- Quorum degradation is the engine's standard behavior in every host: record-verdict recounts usable seats and exits 3, with no host-specific override (inferred: engine behavior, not host-specific)

## Seat execution

- Panel prep is host-truthful: review-prep under this host emits host codex, classifies opus as an external claude-channel subprocess seat, and stamps seat_channels with real provenance (verified: probe 2026-08-12 S1/P5, codex 0.147.0)
- The claude CLI resolves on PATH but is NOT authenticated inside the Codex sandbox: claude -p answers "Not logged in", so external claude seats fail closed with truthful envelopes, ok false, channel claude, real run identity stamps (verified: probe 2026-08-12 S1/P5, codex 0.147.0)
- Nested codex exec is sandbox-walled: the codex seat fails with "failed to initialize in-process app-server client: Operation not permitted"; no approval prompt materialized, so the wall is sandbox-level, not a grantable permission (verified: probe 2026-08-12 S1/P4, codex 0.147.0)
- cursor-agent seats are unavailable in this host: doctor reports the agent binary found but not recognized as Cursor Agent; no keychain grant was ever prompted (verified: probe 2026-08-12 S1/P0 and S1/P4, codex 0.147.0)
- Approval persistence is unmeasured: no grant was ever offered to persist, so the persistence rows stay open (unverified)
- Missing-CLI behavior is the explicit skip envelope: with claude hidden from PATH, prep names the seat in a stderr note, the run lands ok false with error "skipped: claude not found on PATH", and collect renders the labeled SKIPPED block (verified: probe 2026-08-12 S1/P5, codex 0.147.0)
- Version-manager shims break isolated-PATH work in this host: a which python3 that returns an asdf shim fails outside the manager's PATH, so pinned-PATH procedures must symlink sys.executable instead (verified: probe 2026-08-12 S1/P5, codex 0.147.0)

## Probe log

- 2026-08-12 S1, codex 0.147.0: P0 preamble (locate, doctor, both host checks, gate propagation, project-root reading), P1 first env capture, P2 payload-key capture over two turns, P4 two-seat approval probe, P5 live gate (pong denied by sandbox auth, external-seat fail-closed envelopes, detection read, missing-CLI skip path), P3 stop-block probe (three attempts, all coerced), wrap-up clean
- 2026-08-12 S2, codex 0.147.0: second env capture, emulation booleans, claude on PATH confirmed
