# Operation and configuration

All settings use the `TITLER_` prefix and load from environment variables or `.env`; environment
variables win. Copy `.env.example` for documented initial values. JSON arrays/objects configure
`STDIO_COMMAND` and `HELPER_CONFIG`. A CLI `--dry-run` overrides the environment to true.
Use a dedicated writable SQLite path; its parent directory must exist. Treat the database
and candidate logs as conversation metadata. Keep bearer tokens in the environment, not
command-line arguments or version control. Use `wss://` for remote authenticated endpoints.

## One-shot scans

```sh
uv run codex-titler scan --once --dry-run
uv run codex-titler scan --once
```

Both commands use the same configuration, thresholds, history acquisition, generation,
helper exclusions, cooldowns, final title recheck, and rename safeguards as the daemon.
The CLI dry-run flag overrides configuration to true; omitting it preserves `TITLER_DRY_RUN`.
Existing `codex-titler` and `codex-titler --dry-run` commands still run continuously.

One-shot mode enumerates all pages of non-archived and archived threads, using the daemon's
source-kind/provider scope. IDs are deduplicated across the entire run. Queue admission waits
for capacity, and completion waits for all admitted jobs, even after a later scan page fails.
There are no periodic rescans, reconnect loops, or source-title notification scheduling;
transport and helper generation notifications remain active. A stale or newly ineligible
thread is skipped, without dirty-thread rescheduling. Run again to consider later changes.
The scan is not an atomic snapshot of the thread list.

The final JSON `scan_completed` event reports unique `scanned` IDs, confirmed `renamed`
(including successful read-back after a lost response), `dry_run` candidates, `skipped`
(ineligible, helper, cooldown, or stale), `failed` jobs, `unresolved` writes, `errors`
(scan/setup/connection/cleanup failures), and `scan_complete`. Duplicate rows add no counts.
Exit status is `0` for a completed scan without errors, including empty scans and scans with
no eligible threads; `1` for setup errors, incomplete enumeration, processing errors, lost
connections, or uncertain writes; `130` for SIGINT/SIGTERM cancellation. An interrupted run
may have unfinished IDs not represented by terminal outcome counts. Failures before a
connection/scan can start are logged as startup errors and may have no completion summary.

Per-read, invalid-output, and context-limit retries remain bounded as described below.
Failed jobs are not retried by rescheduling during this run. An uncertain rename is never
replayed; it is checked by reading the current title, and a failed verification is an error.
Dry-run never calls `thread/name/set`, but it persists candidates and applies the same
`COOLDOWN` (default 300 seconds) for the original title hash. An immediate subsequent real
run may therefore skip those titles; wait for the cooldown before running it. A changed
title hash can be eligible immediately. Keep the same SQLite database between invocations.
Workers, known helper turns, transport, and SQLite are cleaned up on completion or interruption.

## Limits and retries

`CONTEXT_LIMIT` is required: set it to an operator-verified effective limit for this
app-server/model/account. The example 32768 is illustrative, not an advertised model maximum.
The smaller of context and configured auto-compaction limits is used, minus output/reasoning
reserve, server-added overhead, and safety margin. Helper compaction is configured consistently
with these settings. Policies and server-added instructions may still reduce usable capacity.

The estimator counts UTF-8 bytes as estimated tokens. It is deliberately conservative for
ordinary text and includes serialized content, title instructions, JSON formatting, and visible
wrappers. It is **not an exact tokenizer or a mathematical bound on unknown server framing**.
No arbitrary model-to-tiktoken encoding is selected. `Estimator` can be replaced once encoding
compatibility is verified. Raise overhead/margin when deployment evidence warrants it.
A context rejection halves the input budget and starts a fresh helper from the same snapshot,
up to `CONTEXT_RETRIES=2`. Invalid output has one regeneration retry by default. These limits
are independent and finite; no previous prompt accumulates in a retry's context.

Safe read operations retry transient timeouts/connection failures up to `READ_RETRIES` with
bounded exponential delay (maximum 10 seconds). Reconnection uses `BACKOFF` seconds and performs
a full scan. Model unavailability/unsupported effort is a startup error, with no fallback.
Other per-job failures use `COOLDOWN` and periodic reevaluation, including an unchanged failed
title hash. Dry-run also cools down repeated candidates. A new title hash is immediately eligible.
If a rename response is lost, read the title; a matching candidate confirms the outcome.
A failed verification leaves the outcome uncertain and lets a later scan revalidate.
Thread creation and turn start are never automatically replayed after ambiguous failure.

## Resource bounds and recovery

In daemon mode, `QUEUE_SIZE` bounds pending IDs; overflow is logged and recovered on later scans. Duplicate
events coalesce, rapid events reset the debounce deadline, and workers own one source ID at a
time. `CONCURRENCY` bounds complete repair jobs and thus model work. Snapshot acquisition stays
complete even when it exceeds model input capacity: memory is proportional to acquired history,
not to context size. The 64 MiB default WebSocket/stdio message limit is configurable. If a
history response exceeds it, the job fails instead of claiming a complete read; adjust the limit
with available memory in mind. A single response or history can still exhaust process memory.

SQLite records job IDs, source IDs, original-title hashes, snapshot hashes, candidates, job
states, generation-attempt counts, acquisition times, outcomes, retry deadlines, and timestamps.
Raw transcripts are held in memory, not persisted or logged. Helper IDs are persisted to avoid
recursive repair; ephemeral metadata is also excluded. Interrupted active states become
`interrupted` on restart; a fresh metadata/history read must precede any new rename. Existing
candidates are never replayed. Keep the database across restarts; no distributed coordination
is provided. Run only one daemon per state/scope. Historical job/helper rows are retained for
investigation; stop the daemon before manually pruning old records or backing up SQLite.

SIGINT/SIGTERM cancel workers, interrupt known helper turns, unsubscribe known helpers, close
transport, and close SQLite. Cleanup calls have request timeouts. If a creation/turn-start
response was lost, the client may not know the resource ID and cannot interrupt it explicitly;
connection closure and server inactivity handling are the remaining cleanup mechanism. The
source conversation is never interrupted, resumed, archived, or rewritten by this client.

Logs are JSON with job/source IDs, hashes, acquisition duration, generation attempts, budget,
truncation, outcomes and retry reasons. Full prompts and transcripts are not logged. Dry-run
logs candidate strings intentionally. Unexpected required protocol data is scoped to a job,
scan, or connection; unknown notifications/extra metadata are ignored. Failures remain retryable
under cooldown unless startup compatibility verification fails.
