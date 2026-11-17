# Usage and configuration

Install Python 3.12+ and uv, then run `uv sync --locked`. The lock includes runtime and separate development dependencies; `uv sync --locked --no-dev` installs runtime only. The executable is `codex-auto-title`.

Connect to the same listening App Server process used by the UI whose threads should be observed. Sharing a Codex home directory with an unrelated server does not supply its live notifications. This utility does not start a server or discover UI connections automatically.

```toml
[titler]
endpoint = "unix:///absolute/path/to/app-server.sock"
unix_uri = "ws://localhost/"
provider = "openai"
ephemeral_project_path = "/tmp"
request_timeout = 30
generation_timeout = 180
workers = 2
max_message_bytes = 67108864
max_title_length = 80
safety_tokens = 8192
output_reserve = 4096
context_retries = 2
generation_attempts = 3
transient_failures = 3
estimation_policy = "utf8_bytes" # or "exact_only"
estimate_multiplier = 1.25
context_fraction = 0.8
poll_seconds = 10
reconnect_max = 30
max_pages = 10000
```

```sh
codex-auto-title --config titler.toml
codex-auto-title --config titler.toml --endpoint ws://127.0.0.1:4500
codex-auto-title --endpoint wss://codex.example.com --token-env CODEX_SERVER_TOKEN
```

Configuration is validated with Pydantic; unknown keys and invalid values fail early. CLI values override TOML values, which override defaults. All settings except `endpoint` have defaults. Use `--help` for CLI switches. There is deliberately no model or effort override.

`ephemeral_project_path` sets the project directory for every ephemeral helper,
including context-limit probes and title-generation attempts in both continuous
monitoring and one-time backfill mode. It defaults to the literal `/tmp`, even
without a configuration file. The same directory is used to read the helper's
configuration and discover MCP servers to disable; probe metadata is cached
separately for each project path. Source threads are unaffected.

The path refers to the App Server host, including when the server is remote.
It is passed unchanged to the server without checking local directory existence.
Set it in the TOML file to use a custom directory on that host:

```toml
[titler]
endpoint = "wss://codex.example.com"
ephemeral_project_path = "/srv/codex/title-helpers"
```

`token_env` (CLI `--token-env`) names an environment variable holding a bearer token. It is sent in the HTTP handshake on either transport. Tokens are never loaded from repository `.env` files or printed by this application. Only use `--verbose` with care: third-party WebSocket debug logs can include handshake headers and transcript data.

`unix_uri` controls the HTTP Upgrade URI, independently of the decoded absolute socket path in `endpoint`. For TCP use `ws://HOST:PORT/PATH` or `wss://HOST/PATH`; TLS uses the platform trust store with hostname verification. Unix socket availability and permissions are reported with corrective guidance. Windows needs an available AF_UNIX implementation or TCP.

WebSocket frames are capped at `max_message_bytes` (64 MiB initially). Oversized responses disconnect and defer the incomplete job. Increase the cap if full history pages require it; an individual server item can exceed the cap even with small page sizes. Requests have a deadline covering send plus response; standalone initialization sends, handshakes and generation also have finite timeouts. Ctrl-C cancels workers, attempts helper interruption/unsubscription and closes the connection.

When an eligible unnamed thread first has a running source turn, collection waits
once for five seconds, then reads fresh full history including available items
from running turns. Completion during the wait does not shorten it. The deadline
is retained across retries and reconnects within the daemon process. Once a valid
snapshot contains meaningful user text, it is frozen for generation and context
retries; later source activity is excluded.

The five-second deadline wakes the scheduler independently of `poll_seconds = 10`
and the generic retry delay; connection, worker and RPC availability can affect
when collection actually starts. Other waiting/transient jobs log their phase
and retry no faster than every 30 seconds. Empty/nontext input and retrieval
failures follow their existing deferral policy without restarting the source delay.
Terminal unsupported/exhausted candidates do not retry. `generation_attempts` caps
lifetime probes and title turns separately; `context_retries` and
`transient_failures` never reset on polling or reconnect. The latter also caps
source name writes; unresolved writes retain only compact intent for readback.
CLI switches `--generation-attempts` and `--transient-failures` override these values.
See [candidate policy](behavior.md#candidate-retry-and-retention-policy). There is no encoding alias, model fallback or hard-coded context limit. The default approximation permits generation without a verified tokenizer mapping; missing runtime context still defers. `--estimation-policy`, `--estimate-multiplier` and `--context-fraction` override the TOML values. See the [budget policy](behavior.md#generation) for assumptions and reserves.

To run the opt-in live smoke test (creates and retains one named synthetic source thread, plus ephemeral helpers):

```sh
uv run python tools/verify_live.py --endpoint unix:///absolute/path/to/app-server.sock
```

Use the same endpoint as the intended UI; the script verifies broadcast detection across two real client connections, Japanese title script, frozen snapshot and name readback. Its Japanese-script check is a sanity check, not a universal language classifier. It does not prove notifications from an unrelated server or UI connection.


## One-time backfill

```sh
uv run codex-auto-title --endpoint unix:///absolute/path/to/app-server.sock --backfill-once
# Or use your existing --config FILE with --backfill-once.
```

This opt-in command connects to the configured existing server, collects all pages
of `thread/list` with `archived: false`, deduplicates IDs, then processes that fixed
population with at most `workers` concurrent jobs. It includes historical threads
regardless of startup timestamp or known IDs. It does not enroll `thread/started`
events or run a second scan. Archived threads are excluded; nothing is unarchived.
The scan sees only what that server, account, provider/source defaults and access
permissions expose. It does not search other servers, accounts, or local files.
Pagination is a moving server view, not an atomic snapshot: threads created or
changed during traversal may appear or be missed. After enumeration, new threads
are outside this run.

Only a metadata read with an explicitly null name permits generation. Existing
names, including empty strings, are preserved. Ephemeral helpers, forks, children,
and unknown ephemeral status are excluded. The same full history capture, one-time
five-second running-source delay, language selection, title validation, Luna/low
helpers, concurrency and candidate retry budgets apply. Source threads are only
read and, if still eligible, renamed. A final name reread protects concurrent
manual renames subject to the API's residual read/write race.

Empty/nontext or unmaterialized history is deferred immediately instead of polled
forever. Unsupported history/configuration is deferred; exhausted retry budgets
are failed. Connection/initialization attempts are capped by `transient_failures`,
with exponential delays capped by `reconnect_max`. Once initialized, this mode
does not reconnect or rescan after connection loss: remaining jobs exhaust their
bounded requests or report unresolved writes. At most `transient_failures`
unsuccessful uncertain-write reconciliation passes are allowed per thread.
Reconciliation reads the current name before any further write; an explicit null
can permit a bounded retry of the retained title, while any non-null name resolves
the outcome without replay. No name write is blindly replayed.

If enumeration fails (including repeated cursors or `max_pages` exhaustion), no
threads are renamed; discovered candidates are reported as deferred. The summary
marks `enumeration_complete: false`, and `scanned` is only the unique IDs observed.
On complete enumeration, `scanned` counts all unique returned IDs; `eligible`
counts threads observed with explicit null names and supported thread type in
metadata. It can include threads later manually named or deferred. The terminal
counts `renamed`, `skipped`, `deferred`, `failed`, and `unresolved` partition scanned
threads. Non-renamed outcomes print an ID and concise reason, followed by a JSON
summary. Missing metadata may prevent eligibility from being established.

Exit status is **0** when enumeration completed and every thread was renamed or
skipped; **1** indicates incomplete enumeration, connection failure, deferred,
failed, or unresolved work. CLI/configuration errors use argparse's status **2**.
There is no persisted run state. Running the command again scans again and
preserves any names already set. See [behavior](behavior.md#one-time-backfill-mode)
for limitations.
