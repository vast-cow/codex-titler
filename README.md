# Codex Auto Title

Python 3.12+ utility for naming new, unnamed threads on an **existing** Codex App Server. Generation uses ephemeral App Server threads with exactly `gpt-6-luna`, effort `low`.

```sh
uv sync --locked
uv run codex-auto-title --endpoint unix:///absolute/path/to/app-server.sock
uv run codex-auto-title --endpoint ws://127.0.0.1:4500
uv run codex-auto-title --endpoint wss://codex.example.com --token-env CODEX_SERVER_TOKEN
```

The Unix listener must speak WebSocket, including HTTP Upgrade. It is not a JSON-lines socket. The utility never starts another server. `wss://` uses certificate verification.

Input sizing uses a verified `tiktoken` mapping when available; otherwise the default policy estimates tokens as UTF-8 bytes × 1.25. This is an approximation, not Luna's exact tokenizer or a fit guarantee. Every job must separately discover the effective context limit with a small Luna/low runtime probe before submitting history; no context limit is hard-coded. Additional margins reserve instructions, server context, output and reasoning. Configure `estimation_policy = "exact_only"` to require a verified mapping.

Real-server verification and remaining limitations are recorded in [compatibility and validation](docs/compatibility.md).

CLI options override the `[titler]` table in `--config FILE`. See [configuration and usage](docs/usage.md), [monitoring, snapshots and limitations](docs/behavior.md), and [compatibility and validation](docs/compatibility.md).

```sh
uv run pytest
uv run ruff check src/codex_auto_title tests tools
```
