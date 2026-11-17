import asyncio

import pytest
from test_workflow import FakeRPC, monitor

from codex_auto_title.backfill import Backfill
from codex_auto_title.config import Settings
from codex_auto_title.daemon import Candidate
from codex_auto_title.generation import Generator, Title
from codex_auto_title.history import Deferred, capture
from codex_auto_title.lifecycle import AttemptState
from codex_auto_title.rpc import RPC


class ProbeRPC(FakeRPC):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.servers = {"test": {"enabled": True}}
        self.probes = 0
        self.failure = None
        self.input_tokens = 200
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    def emit(self, method, params):
        if method == "thread/tokenUsage/updated":
            params["tokenUsage"]["total"]["inputTokens"] = self.input_tokens
        super().emit(method, params)

    async def request(self, method, params=None):
        if method == "config/read":
            return {"config": {"mcp_servers": self.servers}}
        if method == "thread/start":
            result = await super().request(method, params)
            result["modelProvider"] = params["modelProvider"]
            return result
        if (
            method == "turn/start"
            and params["input"][0]["text"] == 'Return {"title":"Probe"}.'
        ):
            self.probes += 1
            self.started.set()
            await self.release.wait()
            if self.failure:
                raise self.failure
        return await super().request(method, params)


@pytest.mark.parametrize("mode", ["watch", "backfill"])
async def test_jobs_reuse_metadata_on_connection_even_with_new_generator(mode):
    rpc = ProbeRPC()
    runner = (
        monitor(rpc)
        if mode == "watch"
        else Backfill(Settings(endpoint="ws://localhost"))
    )
    runner.rpc = rpc
    for tid in ("a", "b", "c"):
        rpc.name = None
        runner.generator = Generator(rpc, runner.s)
        runner.candidates[tid] = Candidate()
        await runner.process(tid)
        assert runner.candidates[tid].phase == "named"
        assert runner.generator.last_discovery["context_limit"] == 32000
    assert rpc.probes == 1
    assert rpc.helpers == 4  # One probe and three title turns.
    assert sum(c.attempts.probes for c in runner.candidates.values()) == 1


async def test_effective_key_and_connection_lifetime():
    rpc = ProbeRPC()
    snapshot = await capture(rpc, "s")
    settings = Settings(endpoint="ws://localhost")

    async def generate(**updates):
        await Generator(rpc, settings.model_copy(update=updates)).generate(snapshot)

    await generate()
    # Budget/tokenizer/scheduling settings do not change the helper or probe.
    await generate(
        safety_tokens=100, context_fraction=0.7, estimate_multiplier=2, workers=1
    )
    assert rpc.probes == 1
    await generate(max_title_length=100)
    await generate(provider="other")
    assert rpc.probes == 3
    rpc.servers = {"b": {}, "a": {}}
    await generate()
    assert rpc.probes == 4
    rpc.servers = {"a": {"enabled": True}, "b": {"enabled": False}}
    await generate()  # Same effective disable overrides, independent of order/values.
    assert rpc.probes == 4
    other = ProbeRPC()
    await Generator(other, settings).generate(snapshot)
    assert other.probes == 1
    assert RPC(None).probe_metadata == {}  # Production owner starts empty too.


@pytest.mark.parametrize("failure", [TimeoutError("timeout"), Deferred("unsupported")])
async def test_failed_probe_removed_and_next_job_can_probe(failure):
    rpc = ProbeRPC()
    rpc.failure = failure
    snapshot = await capture(rpc, "s")
    gen = Generator(rpc, Settings(endpoint="ws://localhost"))
    state = AttemptState()
    with pytest.raises(type(failure)):
        await gen.generate(snapshot, state)
    assert state.probes == 1
    assert not rpc.probe_metadata
    rpc.failure = None
    await gen.generate(snapshot)
    assert rpc.probes == 2


@pytest.mark.parametrize("field", ["window", "input_tokens"])
@pytest.mark.parametrize("value", [None, 0, -1, True, "32000"])
async def test_incomplete_probe_not_cached(field, value):
    rpc = ProbeRPC()
    setattr(rpc, field, value)
    snapshot = await capture(rpc, "s")
    gen = Generator(rpc, Settings(endpoint="ws://localhost"))
    with pytest.raises(Deferred, match="runtime"):
        await gen.generate(snapshot)
    assert not rpc.probe_metadata
    rpc.window = 32000
    rpc.input_tokens = 200
    await gen.generate(snapshot)
    assert rpc.probes == 2


async def concurrent_discovery(rpc):
    rpc.servers = {}
    settings = Settings(endpoint="ws://localhost")
    first, second = Generator(rpc, settings), Generator(rpc, settings)
    states = [AttemptState(), AttemptState()]
    rpc.release.clear()
    schema = Title.model_json_schema()
    owner = asyncio.create_task(first.discover(schema, {}, states[0]))
    await rpc.started.wait()
    waiter = asyncio.create_task(second.discover(schema, {}, states[1]))
    await asyncio.sleep(0)  # Let the waiter join the pending probe.
    assert states[1].probes == 1
    assert rpc.probes == 1
    return owner, waiter


async def test_concurrent_missing_metadata_shares_success():
    rpc = ProbeRPC()
    owner, waiter = await concurrent_discovery(rpc)
    rpc.release.set()
    assert await owner == await waiter == (32000, 200)
    assert rpc.probes == 1


async def test_concurrent_failure_shared_and_removed():
    rpc = ProbeRPC()
    rpc.failure = TimeoutError("timeout")
    owner, waiter = await concurrent_discovery(rpc)
    rpc.release.set()
    results = await asyncio.gather(owner, waiter, return_exceptions=True)
    assert all(isinstance(result, TimeoutError) for result in results)
    assert not rpc.probe_metadata
    assert rpc.probes == 1


@pytest.mark.parametrize("cancel_owner", [False, True])
async def test_cancelled_probes_and_waiters_leave_no_stuck_entry(cancel_owner):
    rpc = ProbeRPC()
    owner, waiter = await concurrent_discovery(rpc)
    cancelled = owner if cancel_owner else waiter
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    rpc.release.set()
    if cancel_owner:
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert not rpc.probe_metadata
    else:
        assert await owner == (32000, 200)
    snapshot = await capture(rpc, "s")
    await Generator(rpc, Settings(endpoint="ws://localhost")).generate(snapshot)
    assert rpc.probes == (2 if cancel_owner else 1)
