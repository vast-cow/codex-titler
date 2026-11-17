# Codex Titler

A Python 3.11+ daemon that automatically replaces explicit Codex thread names longer than
120 Unicode graphemes with titles of at most 80 graphemes, using `gpt-6-luna` at `low` effort.

```sh
uv sync --locked
cp .env.example .env
# Edit the endpoint and verified effective context limits in .env.
uv run codex-titler --dry-run
uv run codex-titler
```

Connect to the **existing app-server that owns your threads**, via Unix-socket WebSocket
or `ws://`/`wss://`. It must support the documented paginated thread APIs and offer the
required model/effort. No per-title approval is requested in normal mode. Dry-run generates
and logs candidates but never sets names. SIGINT/SIGTERM stop the daemon.

Each repair reads every available history page before freezing an immutable acquisition
snapshot collected over an interval. Input truncation happens separately. Snapshots are not
atomic, and attachment references do not imply attachment contents were read.

Initial, reconnect, and periodic scans cover archived and non-archived threads across all
known source kinds and providers. Notifications accelerate detection; polling cannot capture
every transient title. A final name recheck reduces conflicts, but the API has no conditional
rename and a race remains.

See [operation and configuration](docs/operations.md),
[protocol verification and remaining live checks](docs/protocol.md), and
[design and testing](docs/design.md). No live Codex endpoint was available during development.

```sh
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
```
