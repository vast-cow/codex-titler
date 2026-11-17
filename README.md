# Codex Auto Title

Python 3.12+ utility for naming new, unnamed threads on an **existing** Codex App Server. Generation uses ephemeral App Server threads with exactly `gpt-6-luna`, effort `low`.

```sh
uv sync --locked
uv run codex-auto-title --endpoint unix:///absolute/path/to/app-server.sock
uv run codex-auto-title --endpoint ws://127.0.0.1:4500
uv run codex-auto-title --endpoint wss://codex.example.com --token-env CODEX_SERVER_TOKEN
```

The Unix listener must speak WebSocket, including HTTP Upgrade. It is not a JSON-lines socket. The utility never starts another server. `wss://` uses certificate verification.

**Current compatibility limitation:** the installed Codex 0.169.0 schema was checked, but pinned `tiktoken` 0.14.0 has no verified mapping for `gpt-6-luna`. Jobs explicitly defer until model-compatible token counting can be established; they do not rename threads using guessed limits or another tokenizer. No live generation compatibility is claimed.

CLI options override the `[titler]` table in `--config FILE`. See [configuration and usage](docs/usage.md), [monitoring, snapshots and limitations](docs/behavior.md), and [compatibility and validation](docs/compatibility.md).

```sh
uv run pytest
uv run ruff check src/codex_auto_title tests tools
```
