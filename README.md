# Codex Titler

A Python 3.11+ daemon that automatically replaces explicit Codex thread names longer than
120 Unicode graphemes with titles of at most 80 graphemes, using `gpt-6-luna` at `low` effort.

```sh
uv sync --locked
cp .env.example .env
# Edit the endpoint and verified effective context limits in .env.
uv run codex-titler --dry-run
uv run codex-titler
# Scan existing threads once and exit, without waiting for rename events.
uv run codex-titler scan --once --dry-run
uv run codex-titler scan --once
```

Connect to the **existing app-server that owns your threads**, via Unix-socket WebSocket
or `ws://`/`wss://`. It must support the documented paginated thread APIs and offer the
required model/effort. No per-title approval is requested in normal mode. Dry-run generates
and logs candidates but never sets names. SIGINT/SIGTERM stop the daemon.

One-shot scans finish enumeration and all discovered work, with bounded queue backpressure.
They emit a JSON `scan_completed` summary and exit `0` on success (including no work), `1` on
errors or unresolved writes, and `130` on interruption. They do not rescan or reconnect.
Dry-run records cooldowns: an immediate real run can skip those unchanged titles until
`TITLER_COOLDOWN` expires (300 seconds by default).

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
