# Monitoring and safety semantics

## Eligible threads

`thread/started` registers candidates. A candidate must have `ephemeral: false`, a previously unknown ID, no fork/parent identity, and `createdAt` at least the next whole Unix second after monitor startup. This deliberately conservative timestamp policy can miss threads created in the startup second or with skewed server clocks. A startup `thread/list` baseline records known IDs. Old resumed threads are excluded even if their name is null. Ephemeral notifications are excluded before creation responses arrive, preventing helper recursion.

Only explicitly null current names are eligible. Empty strings count as names. Missing names after a metadata read cause deferral. The monitor rereads metadata and all available full-detail history for candidates. Empty/nontext-only user input defers. If no source turn is running when first checked, full history with meaningful textual user input permits immediate generation. On first observing an `inProgress` source turn, the candidate records a monotonic deadline five seconds later and discards the preliminary history. At that deadline it begins a fresh read-only history capture, even if the source turn is still running or has completed during the wait. This one-time delay is never shortened, reset or extended by completion, new source activity, retries or reconnects in the same daemon process. The utility does not attempt OCR, audio transcription or external-file loading.

In Codex 0.169.0, `thread/started` is broadcast to initialized clients of that server; live turn items and completion events are sent to thread subscribers. The utility never resumes, subscribes to, interrupts or sends turns to **source** threads. It reads only registered candidates for history and current names. Reconnect repeats initialization, retains registered candidate IDs, captured snapshots and uncertain rename intents in memory, and resumes read-only polling. It does not adopt threads created entirely while disconnected. Restart establishes a fresh baseline; state is not persisted. No API scan turns historical unnamed threads into rename candidates. Other server processes and inaccessible remote history are outside the monitored scope.

One worker per source ID, bounded by `workers`, owns its snapshot and rename intent. Name-update notifications cancel active jobs, except a notification matching the retained generated title or arriving during an uncertain write keeps the worker alive for name readback. Subscriber-only or missed notifications are covered by the final metadata reread. A successful manual rename discards generated output. Codex 0.169.0 has no conditional name-set field: a residual race exists between the reread and `thread/name/set` if another client renames concurrently. Ambiguous writes retain their intended title and reread the name before any later action. A non-null result is accepted without replay; acknowledged writes are also read back before marking the worker complete, and a different non-null name is preserved; if it is still explicitly null a subsequent attempt can set the retained title. No mutation is blindly retried by the RPC client.

## Snapshot and truncation

History uses experimental `thread/turns/list`, `itemsView: full`, descending opaque-cursor pagination. This includes every currently exposed turn and item, including available items in `inProgress` turns and unknown fields/types. Allowing running turns after the deadline does not relax full-detail pagination validation. Entire returned page objects are preserved as immutable JSON text. A snapshot records thread ID, retrieval start/end times, turn/item ID boundaries and SHA-256. Accessors return copies. Partial retrievals, non-full views, repeated cursors, duplicate turns and page-limit exhaustion do not produce snapshots.

Descending retrieval starts at the newest page observed and moves toward older history. Newly appended turns are not chased; a finite maximum page count additionally bounds retrieval. The API provides no server-side atomic snapshot token: concurrent edits, rollbacks or compaction can still affect older pages. Full means everything that this API exposes, not hidden raw provider content, inaccessible history or bytes of referenced files. `thread/items/list` is not additionally required because the selected full turn view includes the available items rather than summary/omitted views.

Once a valid collection contains meaningful textual user input, it is frozen for every generation/context retry and reconnection. Later source activity cannot enter that job. Truncation changes only the input representation; snapshots are retained intact in memory only while generation remains eligible. Complete history is used when it fits. Otherwise deterministic selection favors the first user request, subsequent/recent user messages and agent results over tools/logs, with marked item omissions and bounded prefixes for oversized items. Language evidence is sampled from original user messages before history selection, retaining beginning/end snippets; if the evidence itself is excessive it retains first/latest samples with an omission marker. The helper chooses dominant natural-language request language, latest substantive request on ambiguity, and is instructed to disregard code/logs/quotes. This is model-based language inference, not a guaranteed language classifier. Before renaming, a narrow sanity guard rejects a title lacking Japanese script when user prose is predominantly Japanese after stripping fenced/inline code, quoted lines and common log prefixes. It does not provide universal language identification.

## Candidate retry and retention policy

Candidate state lives in the monitor, independently of each connection's Generator.
Polling and reconnects never replenish it. It is not persisted across process restarts;
a restart establishes a fresh baseline and excludes already-existing threads.

- `source_delay`: a running source turn was observed; fresh collection is due at
  the candidate's one-time five-second deadline. No worker is held during the wait.
- `waiting`: empty/nontext history or an unmaterialized thread before its first user
  message. Read-only polling continues without spending probe/generation attempts
  or transient-failure allowances. These waits and actual retrieval failures do
  not restart an already established source deadline.
- `transient`: transport errors, timeouts and other retryable failures. The lifetime
  allowance is `transient_failures` (default 3), including metadata/history failures.
  It is cumulative, not reset after a successful intermediate call.
- `unsupported`: local policy/configuration failures, missing runtime context,
  invalid generated output, rerouting, or unsupported API/configuration errors.
  This is a terminal deferral: no automatic probe or generation retry. Correct the
  configuration/support issue before monitoring future new threads.
- `exhausted`: a candidate has used its probe, generation, context-retry, transient,
  or name-write allowance. Repeated polling performs no work for terminal candidates.

`generation_attempts` (default 3) caps both total probe attempts and total title-turn
attempts independently for the entire candidate lifecycle. Attempts are charged
before requesting helper creation, so timeouts/disconnects cannot reset the count.
`context_retries` (default 2) additionally caps retries following confirmed context
failures; it does not create another generation budget. A reduced representation's
size ceiling survives transient failures and reconnects. Each new generation cycle
requires a fresh runtime probe; no context value is reused across connections.
Server-managed `willRetry` notifications do not consume new local attempts or
launch helpers. Local tokenizer policy is checked before any model/API call, so
`exact_only` with no mapping starts no probe.

A snapshot is released on every terminal outcome and immediately after successful
title generation. Compact state retains the thread ID, snapshot SHA-256, title,
phase, bounded reason text and counters for deduplication and reporting. Full
history is never needed for name readback. An uncertain name write retains title
intent until an explicit name readback resolves it, including across reconnects.
Acknowledgements or notifications alone do not resolve an uncertain write.
Readback failure may keep read-only reconciliation pending without a finite retry
count; it never restarts generation. At most `transient_failures` name writes are
permitted, and each subsequent write requires a fresh explicit-null readback.
A different non-null name is preserved. No history payload is kept while waiting
for reconciliation.

## Generation

Only `gpt-6-luna`, effort `low`, on the explicitly selected provider is accepted. `model/list` pagination checks model presence and advertised low effort, but supplies no context-window inference. Tokenizer identity is a separate fact from context capacity. First inspect reliable model metadata/implementation evidence; Codex 0.169.0 `model/list` has no tokenizer identity and the installed tiktoken mapping has no Luna entry. No compatible exact encoding has been established from these sources. A successful supported tiktoken model mapping is labeled verified; no other model's encoding is silently substituted.

With `estimation_policy = "utf8_bytes"` (default), unavailable exact mapping uses `ceil(UTF-8 byte length × estimate_multiplier)` for the entire formatted text and schema; the multiplier defaults to 1.25 (minimum 1). This assumes typical byte-based tokenization where bytes are a conservative starting estimate. It is an approximate input-size estimate, not a verified model tokenizer, a measured token count or a guarantee of fitting. Unusual tokenization and hidden server content can violate this assumption. `exact_only` explicitly defers without a verified mapping. Logs identify method and verified status.

A small fresh ephemeral probe uses the same provider, model, effort, output schema and helper configuration. Successful runtime `thread/tokenUsage/updated` must contain positive integer `modelContextWindow` and input-token usage. Missing/invalid values defer. No discovered data is cached across jobs or connections, avoiding reuse across provider/config/auth/server changes. Source-thread context windows are never consulted.

The budget starts with `floor(discovered_window × context_fraction)` (default 0.8), then subtracts observed probe input (including server-added instructions and conservatively including probe text), schema size estimate, `output_reserve` (4096 tokens for output and reasoning) and `safety_tokens` (8192 for hidden/server-added context and estimation error). Local instructions, language evidence and formatting are included in the fully counted representation. Adjust these settings for provider behavior; reducing margins increases overflow risk. Automatic compaction threshold is explicitly placed above any usable context size; this is a compaction setting, not an assumed context capacity. The full formatted prompt is counted and recounted after truncation. Provider maximum-input constraints not exposed separately can still reject an attempt; a terminal failure with structured `codexErrorInfo: contextWindowExceeded` triggers bounded halving of the same snapshot's input representation in a fresh helper. Each retry budget is also at most half the failed prompt estimate so a small full prompt cannot be resubmitted unchanged. Server `error` notifications, including `willRetry: true`, do not launch local attempts: the client waits for terminal `turn/completed`. Authentication, unavailable-model, transport and other failures are not context retries.

Helpers disable shell, web search, apps, multi-agent, code mode, JS REPL, automatic skill instructions and configured MCP servers, use no dynamic tools/environments, and request read-only sandbox with no approvals. Configuration policy may reject overrides, in which case jobs defer. Server-initiated tool/approval requests are rejected with an RPC error. Existing server capabilities and sandbox policy remain server-controlled; there is no universal protocol-level `tools: []` guarantee.

The listener is installed before start requests, so completion, usage and `model/rerouted` notifications arriving before thread/turn acknowledgements are retained. Reroutes are keyed by helper ID and turn ID. Any reroute away from `gpt-6-luna` rejects that turn, even if it later completes successfully or routes back. A rejected probe contributes no context discovery result. This is a terminal model-requirement failure, not a context-size retry. Only successful terminal completion and a final agent answer are accepted. JSON schema requests one `title`, then Pydantic checks exact shape, nonempty trimmed single-line text and configured length. Helpers unsubscribe afterward; unfinished known turns are interrupted on failure/cancellation. If disconnection prevents cleanup, the server controls remaining helper lifetime. No helper is persisted intentionally and source operations remain reads plus the final name update.

## Responsiveness and deadlines

The scheduler wakes asynchronously for candidate deadlines and worker completion,
independently of `poll_seconds` (default 10). The source delay bypasses the generic
minimum 30-second deferred retry interval. Per-thread job exclusivity and the
`workers` limit still apply. The deadline determines when fresh collection is due
to begin; connection, worker and RPC availability can delay actual execution, and
the history API does not promise an atomic snapshot at that instant. Reconnect
cancellation preserves the existing retry/deadline state.

Snapshot serialization/hashing, text-readiness inspection, tokenizer setup, prompt
selection/counting and language validation run via `asyncio.to_thread`. Preparation
parses the snapshot once per representation, serializes each considered item once,
allocates standalone item costs with separator overhead, and stops after filling
the remaining space with a marked prefix. The final complete representation is
recounted to account for non-additive token boundaries. It preserves original
language evidence and immutable snapshot bytes. This reduces repeated whole-prompt
serialization; worker threads still share Python's GIL and this is not a hard
real-time latency guarantee. A cancelled thread job can finish in the background;
its temporary history references are released when that preparation completes.

Each RPC request has one deadline covering send plus response, with pending entries
cleaned on every exit. Standalone sends (including `initialized` and rejection of
server tool requests) are also bounded. A timeout after attempting `thread/name/set`
is uncertain even when the send was blocked; the retained intent is reconciled
before any further write. Initialization and handshakes remain independently
bounded, and generation has its own terminal-turn timeout.
