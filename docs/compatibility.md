# Compatibility evidence

Validation date: 2026-11-17. Starting repository state was clean at `95cbffb`.
Installed and connected App Servers report Codex 0.169.0. No isolated App Server
was started, and no credentials, socket links or server configuration were changed.

## Automated checks

The test suite uses synthetic histories/model metadata/runtime notifications and
loopback TCP and AF_UNIX WebSocket mock servers (including HTTP Upgrade). These
checks cover approximate sizing without a tokenizer mapping, missing runtime
context deferral, frozen snapshots, bounded context retries, non-context errors,
server-managed retries, name races and readback. They do not establish provider
availability. Final validation: **53 tests passed**; Ruff lint and formatting passed.
Run `uv run pytest`, `uv run ruff check src tests tools`, and
`uv run ruff format --check src tests tools`.

The installed binary's experimental schema was checked again with:

```sh
uv run python tools/verify_schema.py /path/to/codex
```

Checked fields include `TurnStartParams.effort`, ephemeral creation, full turn
pagination, name setting, helper unsubscription and
`ThreadTokenUsage.modelContextWindow`. Schema presence alone is not runtime
context evidence.

## Tokenizer and estimation evidence

Installed `tiktoken` 0.14.0 has no `gpt-6-luna` mapping. Its implementation's model
and prefix mapping tables were inspected, rather than treating a lookup failure
as proof that no compatible encoding could exist. The actual `model/list` entry
and 0.169.0 `ModelListResponse` schema were also inspected: they advertise Luna,
including effort `low`, but expose no tokenizer identity. No exact compatible
encoding was established from this evidence. No proxy encoding is claimed as
Luna's tokenizer.

Live generation used **approximate UTF-8 byte length × 1.25**, with 80% of the
runtime window initially available and further subtraction of observed probe
input, schema estimate, 8192 safety tokens and 4096 output/reasoning tokens.
This is conservative input sizing under the documented assumptions, not a fit
guarantee. See [budget policy](behavior.md#generation).

## Existing live endpoints

- `unix:///home/naoto-aoki/Projects/20261117-codex-titler/codex.sock` successfully
  initialized as `codex_desktop/0.169.0` and reported an authenticated account.
- `ws://127.0.0.1:4500` successfully initialized as `codex-web-local/0.169.0` and
  reported an authenticated account.
- `codex-link.sock` remains a dangling symlink to
  `/home/naoto-aoki/.codex/app-server-control/app-server-control.sock` (the parent
  directory is absent). Connection returns `Unix socket missing: check --endpoint
  and the existing server listener`. Use the reachable `codex.sock` endpoint
  above for the desktop server, or port 4500 for the web-local server. Repointing
  the symlink is unnecessary.

Each live smoke test uses a separate observer connection and source-client
connection to the selected existing server. Detection therefore tests real
`thread/started` broadcasts, rather than injecting a local monitor event. A
successful run on one endpoint does not establish visibility of another
process's live UI events. No claim is made that an unrelated UI shares either
connection, nor was a manually clicked UI thread used for this test.

## Actual Unix generation and rename

The opt-in [live verification script](../tools/verify_live.py) created source
`01a1e7ce-8056-7eb0-89f9-ec9a14010f1c`. The observer detected it, deferred before
its first user message, retrieved full terminal-turn history and froze snapshot
SHA-256 `37d14aaa7aa17e270634bb664f2298235f717518b175f4bc9c21a529ee923130`.

Before submitting history, an ephemeral `gpt-6-luna`, effort `low`, provider
`openai` probe returned **258400** effective context tokens and **6881** input
tokens in `thread/tokenUsage/updated`. Provenance: helper
`01a1e7ce-f66e-7dc0-9c24-3346a7a62777`, turn
`01a1e7ce-f6a1-7b73-b5b9-63a44dbdc76d`. Model/provider/effort were confirmed by
`thread/start`. No source-thread context limit was used.

A separate ephemeral Luna/low turn produced
**「Pythonのログ出力による処理速度低下の調査」** from Japanese prose plus forty English
code/log lines. JSON shape, trimming, line count and length passed production
validation; the live script checked Japanese script and the observed title is
Japanese. The source name was reread as null before setting it, and a separate
client read back the exact generated title. Both synthetic source threads are
retained for UI inspection; helper threads were ephemeral.

## Actual TCP generation and rename

The same smoke test against `ws://127.0.0.1:4500` independently detected source
`01a1e7cf-a316-7300-a4b0-1d81205544a8`, deferred empty history, then froze
SHA-256 `fcf557cecbdf9a7182bd91c9267db90dfb3bcd863fd24118a73a66d9e204d407`.
The Luna/low/openai probe again returned **258400** context tokens and **6881**
input tokens, from `thread/tokenUsage/updated` on helper
`01a1e7d0-192f-7fe2-8388-78ca2e5c26e5`, turn
`01a1e7d0-1960-70d3-90fe-794788400fca`.

Actual generation returned **「ログ出力の多いPython処理の性能改善」**. The utility
reread the source's null name, set it, and logged `name readback ... matches=True`.
The independent source client also read back that exact title and passed the
Japanese-script check. The own-name-notification regression fix was active:
the utility's broadcast did not cancel its pending readback.

## Remaining limits

No verified exact Luna tokenizer mapping is available. Language inference is
model based, supplemented by a narrow pre-rename Japanese-script sanity guard
when user prose is predominantly Japanese (ignoring fenced/inline code, quoted
lines and common log prefixes); the live script's Japanese-script check is a sanity check rather
than a universal language detector. The API has no atomic snapshot token or
conditional name-set, leaving the documented retrieval/concurrent-rename races.
Only new candidate threads on the connected server are observed; the utility
cannot infer cross-process event visibility from a shared Codex home directory.
WSS certificate behavior is covered by implementation/configuration checks, not
a live TLS endpoint test.

Official implementation references:

- [0.169.0 thread processor](https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/app-server/src/request_processors/thread_processor.rs)
- [0.169.0 thread lifecycle](https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/app-server/src/request_processors/thread_lifecycle.rs)
- [0.169.0 configuration schema](https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/core/config.schema.json)
- [tiktoken implementation](https://github.com/openai/tiktoken)
