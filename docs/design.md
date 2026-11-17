# Design and validation

`transport` owns connection framing, a single response/event dispatcher, request IDs and
pending futures. `history.Api` owns safe reads, pagination and scope filters. `Snapshot` is
frozen canonical JSON plus acquisition start/completion times; consumers decode copies.
`titles` owns grapheme validation, language evidence and input preparation. `generation`
owns ephemeral helper attempts. `state` owns SQLite recovery/cooldowns; `daemon` owns scheduling
and rename checks. The CLI configures structured logs and signal-driven shutdown.

## Acquisition semantics

The API does not establish transactionally atomic reads. A snapshot represents content returned
over an acquisition interval, including in-progress content as returned. Each page's available
cursor boundaries are retained; turn/item IDs, roles/types, unknown fields, ordering, tool
text and attachment references remain in canonical JSON. Exact duplicate records/items are
removed; distinct versions sharing IDs survive in acquisition order. An intermediate failure
produces no snapshot and no generation. References/paths are never opened as files by the daemon.
Later conversation appends do not invalidate or merge into a job's frozen snapshot. A changed
explicit title does invalidate the candidate and schedules reevaluation in daemon mode.
One-shot mode records a stale outcome and finishes that ID without reevaluation.

## Input policy

The complete serialized acquisition is used when it fits. Otherwise, priority goes to the first
substantive user message, latest substantive user requests, latest agent conclusions, then other
items in reverse recency. Selected content is restored to chronological order. Large items
retain beginning/end grapheme ranges with a middle omission marker. Missing item regions get
explicit omission markers. This reduces large/repetitive tool output before dropping priority
user messages, while full-fit inputs retain tool results. The raw snapshot remains unchanged.
The beginning/end representation of oversized structured items is quoted text, intentionally
not represented as a complete original JSON record.

Language evidence comes only from textual user messages, with fenced code and URLs removed.
User-message language weight and latest-request tie breaking are computed before truncation;
English tool logs never supply language evidence. If absent, the original title is used.
Local language checks are conservative **script heuristics**, strongest for Japanese versus
Latin script; they cannot prove semantic language correctness or distinguish languages sharing
a script. Kanji-only Japanese titles are accepted. The model receives the complete language
policy to resolve mixed languages and substantive intent. This is a documented quality limit,
not a claim of universal language detection. Live multilingual quality evaluation remains needed.

## Automated validation

`uv run pytest -q` uses synthetic conversations and a fake RPC server. It covers explicit
scope filters and archived pagination; repeated metadata scans/events; bounded per-thread work;
full multi-page acquisition with failed intermediate pages; unknown content and snapshot
immutability; title-change rejection; helper exclusion; Unicode graphemes; Japanese user text
versus English logs; full-fit/truncated input budgets; context retries with fresh helpers;
invalid output and unsupported effort; uncertain rename read-back; and restart revalidation.
Transport tests use local TCP and Unix WebSocket servers to exercise actual framing, initialization,
large messages, additive events, server-request rejection and pending-request connection loss.
These tests do not rename real Codex threads. See protocol.md for unrun live checks.

One-shot tests additionally cover queue backpressure beyond capacity, ID deduplication,
eligibility and persisted helper/cooldown filtering, draining queued and in-flight jobs,
dry-run without rename calls, empty/no-work scans, partial enumeration failure, worker
errors, connection loss, uncertain and confirmed writes, stale-title finite completion,
transport cleanup, and backward-compatible CLI flag/configuration handling.
