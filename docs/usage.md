# Usage and configuration

Install Python 3.12+ and uv, then run `uv sync --locked`. The lock includes runtime and separate development dependencies; `uv sync --locked --no-dev` installs runtime only. The executable is `codex-auto-title`.

Connect to the same listening App Server process used by the UI whose threads should be observed. Sharing a Codex home directory with an unrelated server does not supply its live notifications. This utility does not start a server or discover UI connections automatically.

```toml
[titler]
endpoint = "unix:///absolute/path/to/app-server.sock"
unix_uri = "ws://localhost/"
provider = "openai"
request_timeout = 30
generation_timeout = 180
workers = 2
max_message_bytes = 67108864
max_title_length = 80
safety_tokens = 8192
output_reserve = 4096
context_retries = 2
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

`token_env` (CLI `--token-env`) names an environment variable holding a bearer token. It is sent in the HTTP handshake on either transport. Tokens are never loaded from repository `.env` files or printed by this application. Only use `--verbose` with care: third-party WebSocket debug logs can include handshake headers and transcript data.

`unix_uri` controls the HTTP Upgrade URI, independently of the decoded absolute socket path in `endpoint`. For TCP use `ws://HOST:PORT/PATH` or `wss://HOST/PATH`; TLS uses the platform trust store with hostname verification. Unix socket availability and permissions are reported with corrective guidance. Windows needs an available AF_UNIX implementation or TCP.

WebSocket frames are capped at `max_message_bytes` (64 MiB initially). Oversized responses disconnect and defer the incomplete job. Increase the cap if full history pages require it; an individual server item can exceed the cap even with small page sizes. Requests, handshakes and generation have finite timeouts. Ctrl-C cancels workers, attempts helper interruption/unsubscription and closes the connection.

Deferred jobs log their reason and retry no faster than every 30 seconds. There is no automatic tokenizer download, encoding alias, model fallback or hard-coded context limit. With the currently pinned tokenizer, generation is deferred. Updating dependency pins requires re-verifying an explicit model mapping, locking dependencies and running tests.
