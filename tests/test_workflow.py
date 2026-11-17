import asyncio
from types import SimpleNamespace

import pytest
from test_history import HistoryRPC, turn

from codex_auto_title.config import MODEL, Settings
from codex_auto_title.daemon import Candidate, Monitor
from codex_auto_title.generation import GenerationFailure, Generator, Title, tokenizer
from codex_auto_title.history import Deferred
from codex_auto_title.rpc import Disconnected


class FakeRPC(HistoryRPC):
    def __init__(self, *, name=None, text="Build it", window=32000, available=True):
        super().__init__([turn(1, text)])
        self.listeners = set()
        self.closed = asyncio.Event()
        self.probe_metadata = {}
        self.name, self.window, self.available = name, window, available
        self.helpers = 0
        self.ambiguous = False

    def emit(self, method, params):
        for listener in tuple(self.listeners):
            listener(method, params)

    async def request(self, method, params=None):
        self.calls.append((method, params))
        if method == "thread/read":
            return {"thread": {"id": "s", "name": self.name, "ephemeral": False}}
        if method == "config/read":
            return {"config": {"mcp_servers": {"test": {"enabled": True}}}}
        if method == "model/list":
            return {
                "data": [
                    {
                        "model": MODEL,
                        "supportedReasoningEfforts": [{"reasoningEffort": "low"}],
                    }
                ]
                if self.available
                else [],
                "nextCursor": None,
            }
        if method == "thread/start":
            self.helpers += 1
            tid = f"helper{self.helpers}"
            self.emit(
                "thread/started",
                {"thread": {"id": tid, "ephemeral": True, "createdAt": 2**40}},
            )
            return {
                "thread": {"id": tid, "ephemeral": True},
                "model": MODEL,
                "modelProvider": "openai",
                "reasoningEffort": "low",
            }
        if method == "turn/start":
            tid = params["threadId"]
            self.emit(
                "thread/tokenUsage/updated",
                {
                    "threadId": tid,
                    "tokenUsage": {
                        "modelContextWindow": self.window,
                        "total": {"inputTokens": 200},
                    },
                },
            )
            self.emit(
                "turn/completed",
                {
                    "threadId": tid,
                    "turn": {
                        "id": "t",
                        "status": "completed",
                        "items": [
                            {
                                "type": "agentMessage",
                                "text": '{"title":"Build the interface"}',
                                "phase": "final_answer",
                            }
                        ],
                    },
                },
            )
            return {"turn": {"id": "t"}}
        if method == "thread/name/set":
            self.name = params["name"]
            if self.ambiguous:
                raise Disconnected("write outcome unknown")
            return {}
        if method in {"thread/unsubscribe", "turn/interrupt"}:
            return {}
        return await super().request(method, params)


def monitor(rpc):
    m = Monitor(Settings(endpoint="ws://localhost"))
    m.rpc = rpc
    m.generator = Generator(rpc, m.s)
    m.candidates["s"] = Candidate()
    return m


@pytest.mark.parametrize(
    "name,text,expected",
    [("Manual", "hello", True), ("", "hello", True), (None, "", False)],
)
async def test_named_and_empty(name, text, expected):
    rpc = FakeRPC(name=name, text=text)
    m = monitor(rpc)
    await m.process("s")
    assert m.candidates["s"].done is expected
    assert not rpc.helpers
    assert not any(k == "thread/name/set" for k, _ in rpc.calls)


def test_candidate_policy():
    m = Monitor(Settings(endpoint="ws://localhost"))
    m.known.add("known")
    for tid, ephemeral, created in [
        ("known", False, m.since),
        ("old", False, 1),
        ("helper", True, m.since),
        ("new", False, m.since),
    ]:
        m.event(
            "thread/started",
            {"thread": {"id": tid, "ephemeral": ephemeral, "createdAt": created}},
        )
    assert list(m.candidates) == ["new"]


async def test_generation_before_ack_and_probe(monkeypatch):
    rpc = FakeRPC()
    m = monitor(rpc)
    rpc.listeners.add(m.event)
    await m.process("s")
    assert rpc.name == "Build the interface"
    assert rpc.helpers == 2
    assert list(m.candidates) == ["s"]
    assert all(p["effort"] == "low" for k, p in rpc.calls if k == "turn/start")
    assert len([1 for k, _ in rpc.calls if k == "thread/unsubscribe"]) == 2


@pytest.mark.parametrize("window", [None, 0, -1, "32000", True])
async def test_missing_context(window):
    rpc = FakeRPC(window=window)
    with pytest.raises(Deferred, match="runtime"):
        await Generator(rpc, Settings(endpoint="ws://localhost")).attempt(
            "probe", probe=True
        )


async def test_model_unavailable():
    with pytest.raises(Deferred, match="unavailable"):
        await Generator(
            FakeRPC(available=False), Settings(endpoint="ws://localhost")
        ).verify_model()


def test_unsupported_tokenizer(monkeypatch):
    def unsupported(_):
        raise KeyError("unknown")

    monkeypatch.setattr("tiktoken.encoding_for_model", unsupported)
    counter = tokenizer(Settings(endpoint="ws://localhost"))
    assert not counter.verified
    assert counter("日本語") == 12
    with pytest.raises(Deferred, match="no verified tokenizer"):
        tokenizer(Settings(endpoint="ws://localhost", estimation_policy="exact_only"))


async def test_manual_rename_and_snapshot_stability():
    rpc = FakeRPC()
    m = monitor(rpc)

    async def generate(snap, state):
        before = snap.raw_json
        rpc.turns.append(turn(2, "new activity"))
        rpc.name = "Manual"
        assert snap.raw_json == before
        return "Generated"

    m.generator = SimpleNamespace(generate=generate)
    await m.process("s")
    assert rpc.name == "Manual"
    assert not any(k == "thread/name/set" for k, _ in rpc.calls)


async def test_ambiguous_rename_reconciles_without_replaying():
    rpc = FakeRPC()
    rpc.ambiguous = True
    m = monitor(rpc)

    async def generate(_, state):
        return "Generated"

    m.generator = SimpleNamespace(generate=generate)
    await m.process("s")
    assert not m.candidates["s"].done
    await m.process("s")
    assert m.candidates["s"].done
    assert len([1 for k, _ in rpc.calls if k == "thread/name/set"]) == 1


async def test_name_notification_cancels_pending_job():
    m = monitor(FakeRPC())
    job = asyncio.create_task(asyncio.sleep(10))
    m.jobs["s"] = job
    m.event("thread/name/updated", {"threadId": "s", "threadName": "Manual"})
    with pytest.raises(asyncio.CancelledError):
        await job
    assert m.candidates["s"].done


@pytest.mark.parametrize("text", ["", " ", "two\nlines", " leading", "end\r"])
def test_title_validation(text):
    with pytest.raises(ValueError):
        Title.model_validate({"title": text})


async def test_failed_history_never_generates():
    rpc = FakeRPC()
    rpc.turns[0]["itemsView"] = "summary"
    m = monitor(rpc)
    await m.process("s")
    assert m.candidates["s"].snapshot is None
    assert rpc.helpers == 0


async def test_context_retry_uses_same_snapshot_and_fresh_helper(monkeypatch):
    from codex_auto_title.history import capture

    rpc = FakeRPC(text="日本語の相談をします。" * 2000)
    snapshot = await capture(rpc, "s")
    original = snapshot.raw_json
    gen = Generator(rpc, Settings(endpoint="ws://localhost"))
    attempts = []

    async def attempt(prompt, probe=False, schema=None, overrides=None):
        if probe:
            return 32000, 200
        attempts.append(prompt)
        if len(attempts) == 1:
            rpc.turns.append(turn(2, "LATER USER INPUT MUST NOT ENTER"))
            raise GenerationFailure({"codexErrorInfo": "contextWindowExceeded"})
        return "日本語の相談"

    gen.attempt = attempt
    assert await gen.generate(snapshot) == "日本語の相談"
    assert len(attempts) == 2
    assert len(attempts[1]) < len(attempts[0])
    assert snapshot.raw_json == original
    assert all("LATER USER INPUT" not in p for p in attempts)


async def test_helper_timeout_interrupts_and_unsubscribes():
    class HangingRPC(FakeRPC):
        async def request(self, method, params=None):
            if method == "turn/start":
                self.emit(
                    "turn/started",
                    {"threadId": params["threadId"], "turn": {"id": "running"}},
                )
                return {"turn": {"id": "running"}}
            return await super().request(method, params)

    rpc = HangingRPC()
    gen = Generator(rpc, Settings(endpoint="ws://localhost", generation_timeout=0.01))
    with pytest.raises(TimeoutError):
        await gen.attempt("probe")
    assert any(k == "turn/interrupt" and p["turnId"] == "running" for k, p in rpc.calls)
    assert any(k == "thread/unsubscribe" for k, _ in rpc.calls)
    assert not rpc.listeners


async def test_session_reinitializes_without_adopting_historical_threads():
    class SessionRPC(FakeRPC):
        def __init__(self):
            super().__init__()
            self.initialized = 0

        async def initialize(self):
            self.initialized += 1
            self.closed.set()
            return {"userAgent": "synthetic"}

        async def request(self, method, params=None):
            if method == "thread/list":
                return {"data": [{"id": "historical"}], "nextCursor": None}
            return await super().request(method, params)

    rpc = SessionRPC()
    m = monitor(rpc)
    m.candidates["s"].title = "uncertain retained title"
    m.candidates["s"].attempts.probes = 1
    m.candidates["s"].attempts.generations = 2
    m.candidates["s"].attempts.context_retries = 1
    await m.session(rpc)
    await m.session(rpc)
    assert rpc.initialized == 2
    assert m.candidates["s"].attempts.probes == 1
    assert m.candidates["s"].attempts.generations == 2
    assert m.candidates["s"].attempts.context_retries == 1
    assert m.candidates["s"].title == "uncertain retained title"
    assert "historical" in m.known
    assert "historical" not in m.candidates
    assert not rpc.listeners


async def test_approximate_path_generates_and_reads_back(monkeypatch):
    def unsupported(_):
        raise KeyError("unknown")

    monkeypatch.setattr("tiktoken.encoding_for_model", unsupported)
    rpc = FakeRPC()
    m = monitor(rpc)
    await m.process("s")
    assert m.candidates["s"].done
    assert rpc.name == "Build the interface"
    assert rpc.helpers == 2
    assert rpc.calls[-1][0] == "thread/read"
    assert m.generator.last_discovery["context_limit"] == 32000


async def test_generate_defers_without_context_even_with_approximation(monkeypatch):
    def unsupported(_):
        raise KeyError("unknown")

    monkeypatch.setattr("tiktoken.encoding_for_model", unsupported)
    rpc = FakeRPC(window=None)
    m = monitor(rpc)
    await m.process("s")
    assert m.candidates["s"].done
    assert m.candidates["s"].phase == "unsupported"
    assert rpc.helpers == 1
    assert m.candidates["s"].snapshot is None
    assert not any(k == "thread/name/set" for k, _ in rpc.calls)


@pytest.mark.parametrize(
    "info", ["unauthorized", "modelNotFound", "connectionFailed", None]
)
async def test_unrelated_failures_do_not_context_retry(info):
    rpc = FakeRPC()
    m = monitor(rpc)
    attempts = []

    async def attempt(prompt, probe=False, **kwargs):
        if probe:
            return 32000, 200
        attempts.append(prompt)
        raise GenerationFailure(
            {"codexErrorInfo": info, "message": "contextWindowExceeded in quoted text"}
        )

    m.generator.attempt = attempt
    await m.process("s")
    assert len(attempts) == 1


async def test_server_retry_notification_does_not_duplicate_helpers():
    class RetryingRPC(FakeRPC):
        async def request(self, method, params=None):
            if method == "turn/start":
                self.emit(
                    "error",
                    {
                        "threadId": params["threadId"],
                        "turnId": "t",
                        "willRetry": True,
                        "error": {"codexErrorInfo": "contextWindowExceeded"},
                    },
                )
            return await super().request(method, params)

    rpc = RetryingRPC()
    m = monitor(rpc)
    await m.process("s")
    assert rpc.helpers == 2
    assert m.candidates["s"].done


async def test_own_name_broadcast_keeps_worker_alive_for_readback():
    class EchoRPC(FakeRPC):
        async def request(self, method, params=None):
            result = await super().request(method, params)
            if method == "thread/name/set":
                self.emit(
                    "thread/name/updated", {"threadId": "s", "threadName": self.name}
                )
            return result

    rpc = EchoRPC()
    m = monitor(rpc)
    rpc.listeners.add(m.event)
    job = asyncio.create_task(m.process("s"))
    m.jobs["s"] = job
    await job
    assert m.candidates["s"].done
    assert rpc.calls[-1][0] == "thread/read"


async def test_japanese_language_guard_ignores_english_code_before_rename():
    rpc = FakeRPC(
        text="処理速度を改善したいです。計測方法について相談します。\n```python\n"
        + 'logger.info("Waiting for worker completion")\n' * 100
        + "```"
    )
    m = monitor(rpc)
    await m.process("s")
    assert m.candidates["s"].snapshot is None
    assert m.candidates["s"].done
    assert m.candidates["s"].phase == "unsupported"
    assert not any(k == "thread/name/set" for k, _ in rpc.calls)


@pytest.fixture
def source_clock(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(
        "codex_auto_title.daemon.time",
        SimpleNamespace(monotonic=lambda: now[0], time=lambda: 0),
    )
    return now


@pytest.mark.parametrize("completed", [False, True])
async def test_one_time_delay_fresh_capture(completed, source_clock):
    rpc = FakeRPC()
    rpc.turns[0]["status"] = "inProgress"
    m = monitor(rpc)
    await m.process("s")
    c = m.candidates["s"]
    assert c.capture_deadline == c.retry_at == 105
    assert c.snapshot is None and not rpc.helpers
    history_calls = len([k for k, _ in rpc.calls if k == "thread/turns/list"])
    rpc.turns[0]["items"].append(
        {"id": "during", "type": "futureItem", "payload": "during wait"}
    )
    rpc.turns.insert(0, turn(2, "Request added during wait"))
    if completed:
        rpc.turns[1]["status"] = "completed"
    source_clock[0] = 104
    await m.process("s")
    assert c.capture_deadline == c.retry_at == 105
    assert len([k for k, _ in rpc.calls if k == "thread/turns/list"]) == history_calls

    async def generate(snapshot, state):
        assert "Request added during wait" in snapshot.raw_json
        assert "during wait" in snapshot.raw_json
        assert (snapshot.turns[0]["status"] == "completed") is completed
        return "Generated"

    m.generator = SimpleNamespace(generate=generate)
    source_clock[0] = 105
    await m.process("s")
    assert c.done and rpc.name == "Generated"
    assert c.capture_deadline == 105


async def test_delay_and_frozen_snapshot_survive_reconnect(source_clock):
    rpc = FakeRPC()
    rpc.turns[0]["status"] = "inProgress"
    m = monitor(rpc)
    await m.process("s")
    c = m.candidates["s"]

    # Reinitializing a connection must retain candidate timing and retry state.
    class ReconnectRPC(FakeRPC):
        async def initialize(self):
            self.closed.set()
            return {}

    await m.session(ReconnectRPC())
    assert c.capture_deadline == c.retry_at == 105
    fresh = FakeRPC(text="Fresh after reconnect")
    fresh.turns[0]["status"] = "inProgress"
    m.rpc = fresh
    captured = []

    async def generate(snapshot, state):
        captured.append(snapshot)
        raise Disconnected("generation interrupted")

    m.generator = SimpleNamespace(generate=generate)
    source_clock[0] = 105
    await m.process("s")
    assert c.snapshot is captured[0]
    assert c.capture_deadline == 105
    assert "Fresh after reconnect" in c.snapshot.raw_json
    frozen = c.snapshot.raw_json
    await m.session(ReconnectRPC())
    fresh.turns.insert(0, turn(2, "Later activity"))
    m.rpc = fresh

    async def retry(snapshot, state):
        assert snapshot is captured[0]
        assert snapshot.raw_json == frozen
        assert "Later activity" not in snapshot.raw_json
        return "Generated"

    m.generator = SimpleNamespace(generate=retry)
    calls = len(fresh.calls)
    await m.process("s")
    assert not any(k == "thread/turns/list" for k, _ in fresh.calls[calls:])
    assert c.done and c.capture_deadline == 105


async def test_manual_name_during_delay_prevents_capture(source_clock):
    rpc = FakeRPC()
    rpc.turns[0]["status"] = "inProgress"
    m = monitor(rpc)
    await m.process("s")
    rpc.name = "Manual"
    rpc.calls.clear()
    source_clock[0] = 105
    await m.process("s")
    assert m.candidates["s"].done
    assert all(k == "thread/read" for k, _ in rpc.calls)


async def test_failed_delayed_capture_does_not_restart_delay(source_clock):
    rpc = FakeRPC()
    rpc.turns[0]["status"] = "inProgress"
    m = monitor(rpc)
    await m.process("s")
    source_clock[0] = 105
    request = rpc.request

    async def unavailable(method, params=None):
        if method == "thread/turns/list":
            raise Disconnected("history unavailable")
        return await request(method, params)

    rpc.request = unavailable
    await m.process("s")
    c = m.candidates["s"]
    assert c.capture_deadline == 105 and c.retry_at == 135
    assert c.snapshot is None and not rpc.helpers
    rpc.request = request
    rpc.turns[0]["itemsView"] = "summary"
    source_clock[0] = 135
    await m.process("s")
    assert c.done and c.phase == "unsupported"
    assert c.snapshot is None and not rpc.helpers
    assert c.capture_deadline == 105


async def test_default_polling_wakes_at_capture_deadline():
    import time

    class ScheduledRPC(FakeRPC):
        async def initialize(self):
            return {}

        async def request(self, method, params=None):
            if method == "thread/name/set":
                self.closed.set()
            return await super().request(method, params)

    rpc = ScheduledRPC()
    rpc.turns[0]["status"] = "inProgress"
    m = monitor(rpc)
    m.baselined = True
    captured_at = []
    request = rpc.request

    async def record(method, params=None):
        if method == "thread/turns/list":
            captured_at.append(time.monotonic())
        return await request(method, params)

    rpc.request = record
    assert m.s.poll_seconds == 10
    await asyncio.wait_for(m.session(rpc), 8)
    assert len(captured_at) == 2
    deadline = m.candidates["s"].capture_deadline
    assert deadline is not None
    assert 0 <= captured_at[1] - deadline < 1
    assert rpc.name == "Build the interface"


@pytest.mark.parametrize("kind", ["userMessage", "futureImageInput"])
async def test_empty_delayed_capture_retries_without_another_delay(kind, source_clock):
    rpc = FakeRPC(text="")
    rpc.turns[0]["status"] = "inProgress"
    rpc.turns[0]["items"][0]["type"] = kind
    m = monitor(rpc)
    await m.process("s")
    source_clock[0] = 105
    await m.process("s")
    c = m.candidates["s"]
    assert c.phase == "waiting" and c.snapshot is None
    assert c.capture_deadline == 105 and c.retry_at == 135
    assert not rpc.helpers and not c.failures
    rpc.turns.insert(0, turn(2, "Meaningful input"))
    source_clock[0] = 135
    await m.process("s")
    assert c.done and c.capture_deadline == 105


async def test_disconnect_cancellation_does_not_postpone_capture(source_clock):
    rpc = FakeRPC()
    rpc.turns[0]["status"] = "inProgress"
    m = monitor(rpc)
    await m.process("s")
    c = m.candidates["s"]
    entered = asyncio.Event()
    request = rpc.request

    async def blocked(method, params=None):
        if method == "thread/turns/list":
            entered.set()
            await asyncio.Future()
        return await request(method, params)

    rpc.request = blocked
    source_clock[0] = 105
    job = asyncio.create_task(m.process("s"))
    await entered.wait()
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    assert c.capture_deadline == c.retry_at == 105
    assert c.snapshot is None
    rpc.request = request
    await m.process("s")
    assert c.done
