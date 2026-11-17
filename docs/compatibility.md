# Compatibility evidence

Validation date: 2026-11-17. Installed binary: `codex-cli 0.169.0` (standalone Linux x86_64 musl release). The binary was discovered outside PATH. No new server process was started.

The experimental schema was generated directly with:

```sh
/path/to/codex app-server generate-json-schema --experimental --out /tmp/codex-schema
uv run python tools/verify_schema.py /path/to/codex
```

The checked fields include `TurnStartParams.effort` (not response-only `reasoningEffort`), ephemeral thread creation, full turn pagination, name setting, helper unsubscription and `ThreadTokenUsage.modelContextWindow`. Re-run the verification command when changing Codex versions. Field verification does not establish live access to a model or authenticate to a server.

Official references consulted:

- [App Server documentation](https://developers.openai.com/codex/app-server): initialization, lifecycle, full history and name updates.
- [0.169.0 thread processor](https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/app-server/src/request_processors/thread_processor.rs): thread-start broadcast and helper creation.
- [0.169.0 thread lifecycle](https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/app-server/src/request_processors/thread_lifecycle.rs): subscriber-scoped turn notifications.
- [0.169.0 configuration schema](https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/core/config.schema.json): helper feature and compaction overrides.
- [tiktoken](https://github.com/openai/tiktoken): installed 0.14.0 `encoding_for_model("gpt-6-luna")` raises `KeyError`. No supported mapping was established.

Validation on Python 3.14: 39 automated tests passed; Ruff lint and formatting checks passed.

Automated tests use actual loopback TCP and Unix WebSocket mock servers (including HTTP Upgrade), plus synthetic history/model/runtime notifications. Synthetic model metadata is not evidence that the real provider offers `gpt-6-luna`, low effort or a particular window. Availability and context require runtime verification; unsupported tokenizer means current production jobs defer before helper generation.

A read-only initialization attempt against the pre-existing `codex-link.sock` could not connect because its target was missing. No live model generation, context discovery or source rename was performed. The default checked path is safe deferral, not claimed end-to-end live title generation.
