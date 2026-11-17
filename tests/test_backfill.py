import asyncio
import json
from contextlib import asynccontextmanager

import pytest
from test_workflow import FakeRPC

from codex_auto_title.backfill import Backfill
from codex_auto_title.cli import load
from codex_auto_title.config import Settings
from codex_auto_title.generation import Generator
from codex_auto_title.history import Deferred, SourceRunning, Waiting
from codex_auto_title.lifecycle import Exhausted
from codex_auto_title.rpc import Disconnected


class ScanRPC(FakeRPC):
    def __init__(self, rows=None, **kwargs):
        super().__init__(**kwargs)
        self.rows = rows or [[thread("s")]]

    async def request(self, method, params=None):
        if method == "thread/list":
            self.calls.append((method, params))
            index = int(params["cursor"] or 0)
            self.emit("thread/started", {"thread": thread("new", createdAt=2**40)})
            return {
                "data": self.rows[index],
                "nextCursor": str(index + 1) if index + 1 < len(self.rows) else None,
            }
        return await super().request(method, params)


def thread(tid, **kwargs):
    return {"id": tid, "ephemeral": False, "createdAt": 1, **kwargs}


def backfill(**kwargs):
    return Backfill(Settings(endpoint="ws://localhost", **kwargs))


async def test_historical_pagination_dedup_fixed_population(capsys):
    rpc = ScanRPC([[thread("s")], [thread("s")]])
    b = backfill()
    b.known.add("s")
    await b.session(rpc)
    assert list(b.candidates) == ["s"]
    assert b.enumeration_complete
    assert b.candidates["s"].phase == "named"
    assert b.eligible == {"s"}
    assert len([k for k, _ in rpc.calls if k == "thread/name/set"]) == 1
    assert [p["cursor"] for k, p in rpc.calls if k == "thread/list"] == [None, "1"]
    assert all(p["archived"] is False for k, p in rpc.calls if k == "thread/list")
    assert not any(k == "thread/resume" for k, _ in rpc.calls)
    assert all(p["threadId"] != "s" for k, p in rpc.calls if k == "turn/start")
    assert b.report() == 0
    assert '"scanned": 1' in capsys.readouterr().out


@pytest.mark.parametrize("name", ["Manual name", ""])
async def test_existing_names_preserved(name):
    rpc = ScanRPC(name=name)
    b = backfill()
    await b.session(rpc)
    assert rpc.name == name
    assert not rpc.helpers
    assert not b.eligible
    assert b.candidates["s"].phase == "ineligible"


async def test_manual_name_before_write():
    class RenamedRPC(ScanRPC):
        async def request(self, method, params=None):
            if method == "thread/read" and self.helpers:
                self.name = "Manual during generation"
            return await super().request(method, params)

    rpc = RenamedRPC()
    b = backfill()
    await b.session(rpc)
    assert b.candidates["s"].phase == "named_elsewhere"
    assert not any(k == "thread/name/set" for k, _ in rpc.calls)


async def test_excluded_types():
    rpc = ScanRPC(
        [
            [
                thread("helper", ephemeral=True),
                thread("fork", forkedFromId="p"),
                thread("child", parentThreadId="p"),
                {"id": "unknown"},
            ]
        ]
    )
    b = backfill()
    await b.session(rpc)
    assert all(c.phase == "ineligible" for c in b.candidates.values())
    assert not any(k == "thread/read" for k, _ in rpc.calls)


async def test_metadata_fork_is_excluded():
    class ForkRPC(ScanRPC):
        async def request(self, method, params=None):
            result = await super().request(method, params)
            if method == "thread/read":
                result["thread"]["forkedFromId"] = "p"
            return result

    rpc = ForkRPC()
    b = backfill()
    await b.session(rpc)
    assert b.candidates["s"].phase == "ineligible"
    assert not rpc.helpers


@pytest.mark.parametrize(
    "kwargs,phase",
    [
        ({"text": ""}, "deferred"),
        ({"available": False}, "unsupported"),
    ],
)
async def test_finite_unusable_history_and_unsupported(kwargs, phase):
    b = backfill()
    await asyncio.wait_for(b.session(ScanRPC(**kwargs)), 1)
    assert b.candidates["s"].phase == phase
    assert b.report() == 1


async def test_uncertain_write_readback_budget_no_replay():
    class UnresolvedRPC(ScanRPC):
        async def request(self, method, params=None):
            if method == "thread/read" and self.name is not None:
                self.calls.append((method, params))
                raise Disconnected("readback unavailable")
            return await super().request(method, params)

    rpc = UnresolvedRPC()
    rpc.ambiguous = True
    b = backfill(transient_failures=2)
    await asyncio.wait_for(b.session(rpc), 1)
    assert b.candidates["s"].phase == "unresolved"
    assert len([k for k, _ in rpc.calls if k == "thread/name/set"]) == 1
    assert b.report() == 1


async def test_ambiguous_write_successful_readback():
    rpc = ScanRPC()
    rpc.ambiguous = True
    b = backfill()
    await b.session(rpc)
    assert b.candidates["s"].phase == "named"
    assert len([k for k, _ in rpc.calls if k == "thread/name/set"]) == 1


async def test_incomplete_enumeration_no_writes():
    rpc = ScanRPC([[thread("s")], [thread("other")]])
    b = backfill(max_pages=1)
    await b.session(rpc)
    assert not b.enumeration_complete
    assert b.candidates["s"].phase == "deferred"
    assert not rpc.helpers
    assert b.report() == 1


async def test_connection_retries_bounded(monkeypatch):
    calls = []

    @asynccontextmanager
    async def unavailable(settings):
        calls.append(settings)
        raise Disconnected("offline")
        yield  # pragma: no cover

    monkeypatch.setattr("codex_auto_title.backfill.connection", unavailable)
    b = backfill(transient_failures=1)
    assert await asyncio.wait_for(b.run(), 1) == 1
    assert len(calls) == 1


async def test_transient_history_exhaustion():
    class BrokenRPC(ScanRPC):
        async def request(self, method, params=None):
            if method == "thread/turns/list":
                raise Disconnected("history unavailable")
            return await super().request(method, params)

    b = backfill(transient_failures=2)
    await asyncio.wait_for(b.session(BrokenRPC()), 1)
    assert b.candidates["s"].phase == "exhausted"
    assert b.candidates["s"].failures == 2


def test_cli_opt_in():
    assert not load(["--endpoint", "ws://localhost"])[0].backfill_once
    assert load(["--endpoint", "ws://localhost", "--backfill-once"])[0].backfill_once


async def test_repeated_cursor_aborts_enumeration():
    class LoopRPC(ScanRPC):
        async def request(self, method, params=None):
            if method == "thread/list":
                return {"data": [thread("s")], "nextCursor": "loop"}
            return await super().request(method, params)

    b = backfill()
    rpc = LoopRPC()
    await asyncio.wait_for(b.session(rpc), 1)
    assert not b.enumeration_complete
    assert "repeated pagination cursor" in b.error
    assert not rpc.helpers


async def test_missing_current_name_defers():
    class MissingRPC(ScanRPC):
        async def request(self, method, params=None):
            result = await super().request(method, params)
            if method == "thread/read":
                del result["thread"]["name"]
            return result

    b = backfill()
    rpc = MissingRPC()
    await b.session(rpc)
    assert b.candidates["s"].phase == "unsupported"
    assert not rpc.helpers


async def test_worker_limit(monkeypatch):
    from codex_auto_title.daemon import Monitor

    active = maximum = 0

    async def process(self, tid):
        nonlocal active, maximum
        active += 1
        maximum = max(active, maximum)
        await asyncio.sleep(0)
        self.candidates[tid].finish("named")
        active -= 1

    monkeypatch.setattr(Monitor, "process", process)
    b = backfill(workers=2)
    await b.session(ScanRPC([[thread(str(i)) for i in range(8)]]))
    assert maximum == 2
    assert all(c.done for c in b.candidates.values())


@pytest.mark.parametrize("stage", ["read", "generation"])
@pytest.mark.parametrize(
    "failure",
    [None, Disconnected, SourceRunning, Waiting, Exhausted, Deferred, ValueError],
)
async def test_manual_name_cancels_inflight_work_and_preserves_outcome(
    stage, failure, monkeypatch, capsys
):
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def outstanding():
        entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            if failure is not None:
                # An operation may fail during cancellation/cleanup after the
                # notification has already made the candidate terminal.
                raise failure("late operation failure") from None
            raise

    class RenamedRPC(ScanRPC):
        async def request(self, method, params=None):
            if method == "thread/read" and params["threadId"] == "other":
                self.calls.append((method, params))
                return {"thread": thread("other", name="Existing other name")}
            if stage == "read" and method == "thread/turns/list":
                await outstanding()
            return await super().request(method, params)

    async def generate(self, snapshot, attempts):
        await outstanding()

    if stage == "generation":
        monkeypatch.setattr(Generator, "generate", generate)
    rpc = RenamedRPC([[thread("s"), thread("other")]])
    b = backfill(workers=1)
    session = asyncio.create_task(b.session(rpc))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert "s" in b.jobs
        rpc.name = "Manual name"
        rpc.emit(
            "thread/name/updated", {"threadId": "s", "threadName": rpc.name}
        )
        await asyncio.wait_for(session, 1)
    finally:
        session.cancel()
        await asyncio.gather(session, return_exceptions=True)

    assert cancelled.is_set()
    assert rpc.name == "Manual name"
    assert not any(k == "thread/name/set" for k, _ in rpc.calls)
    c = b.candidates["s"]
    assert c.done and c.phase == "named_elsewhere"
    assert c.snapshot is None and not c.rename_uncertain
    assert c.failures == 0 and c.reason is None
    # The same worker must continue after candidate cancellation.
    assert b.candidates["other"].phase == "ineligible"
    assert not b.jobs
    assert not rpc.listeners
    assert b.report() == 0
    output = capsys.readouterr().out
    assert "skipped thread=s:" in output
    summary = json.loads(output.splitlines()[-1])
    assert summary["skipped"] == 2
    assert summary["unresolved"] == summary["failed"] == summary["deferred"] == 0


async def test_batch_cancellation_still_propagates():
    entered = asyncio.Event()

    class BlockingRPC(ScanRPC):
        async def request(self, method, params=None):
            if method == "thread/turns/list":
                entered.set()
                await asyncio.Future()
            return await super().request(method, params)

    b = backfill(workers=1)
    rpc = BlockingRPC()
    session = asyncio.create_task(b.session(rpc))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        job = b.jobs["s"]
        session.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(session, 1)
        assert job.cancelled()
        assert not b.jobs
        assert not rpc.listeners
        assert not b.candidates["s"].done
    finally:
        session.cancel()
        await asyncio.gather(session, return_exceptions=True)
