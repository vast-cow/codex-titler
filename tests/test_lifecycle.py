import asyncio
import threading
from types import SimpleNamespace

import pytest
from test_history import HistoryRPC, turn
from test_workflow import FakeRPC, monitor

from codex_auto_title.config import MODEL, Settings
from codex_auto_title.generation import Generator
from codex_auto_title.history import Deferred, capture, representation


async def test_exhausted_context_budget_does_not_restart_on_poll_or_reconnect():
    class ContextRPC(FakeRPC):
        async def request(self, method, params=None):
            if method == "turn/start" and self.helpers > 1:
                self.calls.append((method, params))
                self.emit(
                    "turn/completed",
                    {
                        "threadId": params["threadId"],
                        "turn": {
                            "id": "t",
                            "status": "failed",
                            "items": [],
                            "error": {
                                "codexErrorInfo": "contextWindowExceeded",
                                "message": "too large",
                            },
                        },
                    },
                )
                return {"turn": {"id": "t"}}
            return await super().request(method, params)

    rpc = ContextRPC(text="Improve the interface " * 5000)
    m = monitor(rpc)
    await m.process("s")
    c = m.candidates["s"]
    assert c.done and c.phase == "exhausted"
    assert c.attempts.generations == 3
    assert rpc.helpers == 4  # One probe, three attempts.
    assert c.snapshot is None and c.snapshot_sha256
    calls = len(rpc.calls)
    m.generator = Generator(
        rpc, m.s
    )  # Same candidate survives a fresh connection generator.
    for _ in range(5):
        await m.process("s")
    assert len(rpc.calls) == calls


async def test_probe_failures_bounded_across_cycles():
    class FailingRPC(FakeRPC):
        async def request(self, method, params=None):
            if method == "turn/start":
                raise TimeoutError("transient timeout")
            return await super().request(method, params)

    rpc = FailingRPC()
    m = monitor(rpc)
    for _ in range(8):
        m.generator = Generator(rpc, m.s)
        await m.process("s")
    c = m.candidates["s"]
    assert c.done and c.phase == "exhausted"
    assert c.attempts.probes == 3 and rpc.helpers == 3
    assert c.snapshot is None


async def test_empty_and_running_polling_spends_no_attempts(monkeypatch):
    rpc = FakeRPC(text="")
    m = monitor(rpc)
    for _ in range(5):
        await m.process("s")
    rpc.turns[0] = turn(1)
    rpc.turns[0]["status"] = "inProgress"
    for _ in range(5):
        await m.process("s")
    c = m.candidates["s"]
    assert not c.done and c.phase == "source_delay"
    assert c.failures == c.attempts.probes == c.attempts.generations == 0
    rpc.turns[0]["status"] = "completed"
    await m.process("s")
    assert not c.done
    monkeypatch.setattr(
        "codex_auto_title.daemon.time",
        SimpleNamespace(monotonic=lambda: c.capture_deadline),
    )
    await m.process("s")
    assert c.done and c.phase == "named"


async def test_exact_only_preflight_makes_no_rpc_calls(monkeypatch):
    def missing(_):
        raise KeyError("no mapping")

    monkeypatch.setattr("tiktoken.encoding_for_model", missing)
    rpc = FakeRPC()
    gen = Generator(
        rpc, Settings(endpoint="ws://localhost", estimation_policy="exact_only")
    )
    snap = await capture(rpc, "s")
    rpc.calls.clear()
    with pytest.raises(Deferred, match="no verified tokenizer"):
        await gen.generate(snap)
    assert rpc.calls == []


async def test_completed_and_manually_named_candidates_drop_payload():
    rpc = FakeRPC(text="Build an interface " * 5000)
    m = monitor(rpc)
    await m.process("s")
    c = m.candidates["s"]
    assert c.done and c.snapshot is None and c.snapshot_sha256
    assert c.title == rpc.name
    # Name notification can terminate a candidate during generation as well.
    c.done = False
    c.snapshot = await capture(rpc, "s")
    m.event("thread/name/updated", {"threadId": "s", "threadName": "Manual"})
    assert c.done and c.snapshot is None


async def test_timeout_write_reconciles_without_snapshot_or_regeneration():
    class UncertainRPC(FakeRPC):
        async def request(self, method, params=None):
            result = await super().request(method, params)
            if method == "thread/name/set":
                raise TimeoutError("send may have completed")
            return result

    rpc = UncertainRPC()
    m = monitor(rpc)
    await m.process("s")
    c = m.candidates["s"]
    assert c.rename_uncertain and c.title and c.snapshot is None
    assert not c.done and c.phase == "reconciling"
    helpers = rpc.helpers
    m.generator = Generator(rpc, m.s)
    await m.process("s")
    assert c.done and c.phase == "named"
    assert rpc.helpers == helpers
    assert len([1 for k, _ in rpc.calls if k == "thread/name/set"]) == 1


@pytest.mark.parametrize("probe", [False, True])
@pytest.mark.parametrize("early", [False, True])
async def test_rerouted_helper_rejected_before_or_after_ack(probe, early):
    class ReroutingRPC(FakeRPC):
        async def request(self, method, params=None):
            if method == "turn/start":
                event = {
                    "threadId": params["threadId"],
                    "turnId": "t",
                    "fromModel": MODEL,
                    "toModel": "other-model",
                    "reason": "highRiskCyberActivity",
                }
                if early:
                    self.emit("model/rerouted", event)
                else:
                    asyncio.get_running_loop().call_soon(
                        self.emit, "model/rerouted", event
                    )
                    # Completion must also be asynchronous, after the reroute.
                    asyncio.get_running_loop().call_soon(
                        lambda: asyncio.create_task(
                            super(ReroutingRPC, self).request(method, params)
                        )
                    )
                    return {"turn": {"id": "t"}}
            return await super().request(method, params)

    rpc = ReroutingRPC()
    gen = Generator(rpc, Settings(endpoint="ws://localhost"))
    with pytest.raises(Deferred, match="required model.*not met"):
        await gen.attempt("probe or title", probe=probe)
    assert gen.last_discovery is None
    assert not rpc.listeners


async def test_rerouted_probe_is_terminal_for_candidate():
    class ReroutingRPC(FakeRPC):
        async def request(self, method, params=None):
            if method == "turn/start":
                self.emit(
                    "model/rerouted",
                    {
                        "threadId": params["threadId"],
                        "turnId": "t",
                        "toModel": "other-model",
                    },
                )
            return await super().request(method, params)

    rpc = ReroutingRPC()
    m = monitor(rpc)
    for _ in range(3):
        await m.process("s")
    assert m.candidates["s"].phase == "unsupported"
    assert m.candidates["s"].snapshot is None
    assert rpc.helpers == 1


async def test_large_preparation_keeps_other_async_work_running(monkeypatch):
    snap = await capture(HistoryRPC([turn(1, "Build a parser " * 100000)]), "s")
    original = snap.raw_json
    entered, release = threading.Event(), threading.Event()

    def prepare(*args):
        entered.set()
        assert release.wait(2), "event loop did not advance during preparation"
        return representation(*args)

    monkeypatch.setattr("codex_auto_title.generation.representation", prepare)
    gen = Generator(FakeRPC(), Settings(endpoint="ws://localhost"))
    task = asyncio.create_task(gen.generate(snap))
    try:
        async with asyncio.timeout(1):
            while not entered.is_set():
                await asyncio.sleep(0.001)
        # This callback stands in for unrelated notifications and worker progress.
        progressed = asyncio.Event()
        asyncio.get_running_loop().call_soon(progressed.set)
        await asyncio.wait_for(progressed.wait(), 0.5)
    finally:
        release.set()
    assert await task == "Build the interface"
    assert snap.raw_json == original


async def test_preparation_stops_after_filling_budget():
    snap = await capture(HistoryRPC([turn(n, "text " * 1000) for n in range(100)]), "s")
    calls = 0

    def counter(text):
        nonlocal calls
        calls += 1
        return len(text)

    result = representation(snap, counter, 4000)
    assert len(result) <= 4000
    assert calls < 40  # Does not keep serializing/rendering all 100 oversized items.


async def test_partial_generation_budget_and_smaller_representation_survive_reconnect():
    from codex_auto_title.generation import GenerationFailure

    rpc = FakeRPC(text="Improve the interface " * 5000)
    m = monitor(rpc)
    prompts = []

    async def attempt(prompt, probe=False, **kwargs):
        if probe:
            return 32000, 200
        prompts.append(prompt)
        if len(prompts) == 2:
            raise TimeoutError("transport interrupted")
        raise GenerationFailure({"codexErrorInfo": "contextWindowExceeded"})

    m.generator.attempt = attempt
    await m.process("s")
    c = m.candidates["s"]
    assert not c.done and c.phase == "transient"
    assert c.attempts.generations == 2 and c.attempts.context_retries == 1
    original = c.snapshot.raw_json
    c.snapshot.items[0]["content"][0]["text"] = "not mutable"
    rpc = FakeRPC()
    m.rpc = rpc
    m.generator = Generator(rpc, m.s)
    m.generator.attempt = attempt
    await m.process("s")
    assert c.done and c.phase == "exhausted"
    assert c.attempts.generations == 3 and c.attempts.probes == 2
    assert len(prompts[1]) < len(prompts[0])
    assert len(prompts[2]) == len(prompts[1])
    assert "not mutable" not in original
    await m.process("s")
    assert len(prompts) == 3


async def test_missing_name_readback_retains_uncertain_intent():
    class MissingReadbackRPC(FakeRPC):
        async def request(self, method, params=None):
            result = await super().request(method, params)
            if method == "thread/read" and self.name is not None:
                return {"thread": {"id": "s", "ephemeral": False}}
            return result

    rpc = MissingReadbackRPC()
    m = monitor(rpc)
    await m.process("s")
    c = m.candidates["s"]
    for _ in range(5):
        await m.process("s")
    assert not c.done and c.rename_uncertain and c.title
    assert c.snapshot is None and c.phase == "reconciling"
    assert c.writes == 1 and rpc.helpers == 2


async def test_competing_name_notification_does_not_discard_uncertain_intent():
    rpc = FakeRPC()
    m = monitor(rpc)
    c = m.candidates["s"]
    c.title = "Generated"
    c.rename_uncertain = True
    m.event("thread/name/updated", {"threadId": "s", "threadName": "Manual"})
    assert not c.done and c.rename_uncertain and c.title == "Generated"
    rpc.name = "Manual"
    await m.process("s")
    assert c.done and not c.rename_uncertain
    assert not any(k == "thread/name/set" for k, _ in rpc.calls)


@pytest.mark.parametrize("probe", [True, False])
async def test_reroute_before_thread_creation_ack_is_retained(probe):
    class EarlyRPC(FakeRPC):
        async def request(self, method, params=None):
            result = await super().request(method, params)
            if method == "thread/start":
                self.emit(
                    "model/rerouted",
                    {
                        "threadId": result["thread"]["id"],
                        "turnId": "t",
                        "fromModel": MODEL,
                        "toModel": "other",
                        "reason": "highRiskCyberActivity",
                    },
                )
            return result

    gen = Generator(EarlyRPC(), Settings(endpoint="ws://localhost"))
    with pytest.raises(Deferred, match="rerouted"):
        await gen.attempt("probe or title", probe=probe)
    assert gen.last_discovery is None
