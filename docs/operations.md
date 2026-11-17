# Operation and configuration

All settings use the `TITLER_` prefix and load from environment variables or `.env`; environment
variables win. Copy `.env.example` for documented initial values. JSON arrays/objects configure
`STDIO_COMMAND` and `HELPER_CONFIG`. A CLI `--dry-run` overrides the environment to true.
Use a dedicated writable SQLite path; its parent directory must exist. Treat the database
and candidate logs as conversation metadata. Keep bearer tokens in the environment, not
command-line arguments or version control. Use `wss://` for remote authenticated endpoints.

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

`QUEUE_SIZE` bounds pending IDs; overflow is logged and recovered on later scans. Duplicate
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
