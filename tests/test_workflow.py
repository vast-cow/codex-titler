import asyncio
import json

import pytest

from codex_titler.config import Settings
from codex_titler.daemon import Monitor
from codex_titler.generation import Generator
from codex_titler.history import SOURCES, Api, Snapshot, Thread, serialize
from codex_titler.state import State
from codex_titler.titles import ByteEstimator, graphemes, prepare, validate_title
from codex_titler.transport import RpcError


@pytest.fixture
def settings(tmp_path):
    return Settings(
        context_limit=32000,
        database=tmp_path / "state.sqlite",
        debounce=0.01,
        cooldown=0.01,
        backoff=0.001,
        request_timeout=0.2,
    )


class Fake:
    def __init__(self):
        self.listeners = set()
        self.closed = asyncio.Event()
        self.calls = []
        self.name = "x" * 121
        self.turns = [
            {
                "id": "t",
                "items": [
                    {
                        "id": "u",
                        "type": "userMessage",
                        "content": [{"type": "text", "text": "日本語で処理を改善してください"}],
                    },
                    {"id": "z", "type": "futureThing", "mystery": {"text": "English log " * 10}},
                ],
            }
        ]
        self.helper_count = 0
        self.fail_page = False
        self.rename_lost = False
        self.change_title = False
        self.context_error = False
        self.candidate = '{"title":"処理の改善"}'
        self.efforts = [{"reasoningEffort": "low"}]

    def emit(self, method, **params):
        for callback in tuple(self.listeners):
            callback(method, params)

    async def call(self, method, params):
        self.calls.append((method, params))
        if method == "model/list":
            return {
                "data": [{"model": "gpt-6-luna", "supportedReasoningEfforts": self.efforts}],
                "nextCursor": None,
            }
        if method == "thread/list":
            if params["cursor"] is None:
                return {"data": [{"id": "source", "name": self.name}], "nextCursor": "p2"}
            return {
                "data": [{"id": "helper", "name": self.name, "ephemeral": True}],
                "nextCursor": None,
            }
        if method == "thread/read":
            return {"thread": {"id": params["threadId"], "name": self.name}}
        if method == "thread/turns/list":
            if params["cursor"] is None:
                return {"data": self.turns, "nextCursor": "p2"}
            if self.fail_page:
                raise RpcError({"code": -1, "message": "bad page"})
            return {"data": self.turns + [{"id": "t2", "items": []}], "nextCursor": None}
        if method == "thread/start":
            self.helper_count += 1
            return {"thread": {"id": f"h{self.helper_count}"}, "model": "gpt-6-luna"}
        if method == "turn/start":
            h = params["threadId"]
            if self.change_title:
                self.name = "Changed externally"
            if self.context_error and self.helper_count == 1:
                self.emit(
                    "turn/completed",
                    threadId=h,
                    turn={"id": "gen", "status": "failed", "error": "contextWindowExceeded"},
                )
            else:
                self.emit(
                    "item/completed",
                    threadId=h,
                    turnId="gen",
                    item={
                        "id": "out",
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": self.candidate,
                    },
                )
                self.emit("turn/completed", threadId=h, turn={"id": "gen", "status": "completed"})
            return {"turn": {"id": "gen"}}
        if method == "thread/name/set":
            self.name = params["name"]
            self.emit("thread/name/updated", threadId="source", threadName=self.name)
            if self.rename_lost:
                raise TimeoutError()
        return {}


@pytest.fixture
async def state(settings):
    state = await State().open(settings.database)
    yield state
    await state.close()


async def test_listing_scope_and_pagination(settings):
    rpc = Fake()
    rows = [t async for t in Api(rpc, settings).threads()]
    assert len(rows) == 4
    calls = [p for m, p in rpc.calls if m == "thread/list"]
    assert [p["archived"] for p in calls] == [False, False, True, True]
    assert all(p["sourceKinds"] == SOURCES and p["modelProviders"] == [] for p in calls)


async def test_snapshot_complete_unknown_immutable_and_failure(settings):
    rpc = Fake()
    api = Api(rpc, settings)
    snap = await api.snapshot("source")
    data = json.loads(snap.raw)
    assert len(data["turns"]) == 2
    assert data["turns"][0]["items"][1]["mystery"]
    rpc.turns[0]["items"].append({"id": "new", "type": "unknown"})
    assert '"new"' not in snap.raw
    assert snap.started <= snap.completed
    rpc.fail_page = True
    with pytest.raises(RpcError):
        await api.snapshot("source")


@pytest.mark.parametrize(
    "lost,changed,dry,outcome",
    [
        (False, False, False, "renamed"),
        (True, False, False, "rename_confirmed"),
        (False, True, False, "stale"),
        (False, False, True, "dry_run"),
    ],
)
async def test_repair(settings, state, lost, changed, dry, outcome):
    rpc = Fake()
    rpc.rename_lost, rpc.change_title = lost, changed
    settings.dry_run = dry
    monitor = Monitor(Api(rpc, settings), state, settings)
    await monitor.generator.verify()
    await monitor.repair("source")
    async with state.db.execute("SELECT * FROM jobs") as cur:
        job = await cur.fetchone()
    assert job["outcome"] == outcome
    assert job["snapshot_hash"] and job["candidate"]
    assert sum(m == "thread/name/set" for m, _ in rpc.calls) == (not changed and not dry)
    assert any(m == "thread/unsubscribe" for m, _ in rpc.calls)


async def test_context_retry_fresh_helpers(settings, state):
    rpc = Fake()
    rpc.context_error = True
    gen = Generator(Api(rpc, settings), state, settings, set())
    snap = await gen.api.snapshot("source")
    assert (await gen.generate(snap, rpc.name))[0] == "処理の改善"
    assert rpc.helper_count == 2
    assert len(await state.helpers()) == 2


async def test_invalid_retries_and_model_effort(settings, state):
    rpc = Fake()
    gen = Generator(Api(rpc, settings), state, settings, set())
    rpc.efforts = []
    with pytest.raises(ValueError):
        await gen.verify()
    rpc.candidate = '{"title":""}'
    with pytest.raises(ValueError):
        await gen.generate(await gen.api.snapshot("source"), rpc.name)
    assert rpc.helper_count == 2


async def test_coalescing_scans_helpers_and_per_thread(settings, state):
    rpc = Fake()
    monitor = Monitor(Api(rpc, settings), state, settings)
    monitor.helpers.add("own")
    monitor.notification("thread/name/updated", {"threadId": "own"})
    for _ in range(10):
        await monitor.scan()
        monitor.notification("thread/name/updated", {"threadId": "source"})
    assert monitor.queue.qsize() == 1
    running, maximum = 0, 0

    async def repair(thread_id):
        nonlocal running, maximum
        running += 1
        maximum = max(maximum, running)
        monitor.schedule(thread_id)
        await asyncio.sleep(0.01)
        running -= 1

    monitor.repair = repair
    workers = [asyncio.create_task(monitor.worker()) for _ in range(2)]
    await asyncio.sleep(0.06)
    for task in workers:
        task.cancel()
    await asyncio.gather(*workers, return_exceptions=True)
    assert maximum == 1


async def test_recovery_and_cooldown(settings, state):
    key = await state.begin("source", "old")
    await state.update(key, state="renaming", candidate="New")
    await state.close()
    await state.open(settings.database)
    async with state.db.execute("SELECT state FROM jobs WHERE id=?", (key,)) as cur:
        assert (await cur.fetchone())[0] == "interrupted"
    assert await state.allowed("source", "old")


def test_graphemes_language_budget(settings):
    assert graphemes("👨‍👩‍👧‍👦e\u0301🇯🇵") == 3
    turns = [
        {
            "id": "t",
            "items": [
                {
                    "id": "u",
                    "type": "userMessage",
                    "content": [{"type": "text", "text": "日本語でアプリを改善してください"}],
                },
                {"id": "log", "type": "commandExecution", "output": "English tool log\n" * 10000},
                {"id": "last", "type": "agentMessage", "text": "作業が完了しました"},
            ],
        }
    ]
    snap = Snapshot(serialize({"turns": turns}), 0, 1)
    prompt, expected, truncated = prepare(snap, "Old title", 80, 3000)
    assert expected == "Japanese" and truncated
    assert ByteEstimator().count(prompt) <= 3000
    assert "omitted" in prompt and "日本語" in prompt and "完了" in prompt
    assert "English tool log" in snap.raw
    small = Snapshot(serialize({"turns": [{"id": "t", "items": turns[0]["items"][:1]}]}), 0, 1)
    assert prepare(small, "Old", 80, 3000)[2] is False
    with pytest.raises(ValueError):
        validate_title('{"title":"English logs"}', 80, expected)


@pytest.mark.parametrize(
    "raw", ['{"title":"a\\nb"}', '{"title":"- choice"}', '{"title":"x","other":1}', '{"title":123}']
)
def test_bad_candidates(raw):
    with pytest.raises(ValueError):
        validate_title(raw, 80, "unknown")


async def test_periodic_reconciliation_recovers_missed_event(settings, state):
    rpc = Fake()
    rpc.name = "Initially short"
    settings.reconcile_interval = 0.02
    monitor = Monitor(Api(rpc, settings), state, settings)
    task = asyncio.create_task(monitor.run())
    await asyncio.sleep(0.03)
    rpc.name = "Long changed without notification " * 8
    for _ in range(100):
        if rpc.name == "処理の改善":
            break
        await asyncio.sleep(0.005)
    rpc.closed.set()
    await asyncio.wait_for(task, 1)
    assert rpc.name == "処理の改善"
    assert sum(m == "thread/list" for m, _ in rpc.calls) >= 8


async def test_reconnect_repeats_full_scan(settings, state, monkeypatch):
    from codex_titler import daemon

    connections = []

    class Connection(Fake):
        server_info = {"userAgent": "fake"}

        async def open(self):
            pass

        async def close(self):
            self.closed.set()

    def factory(_):
        rpc = Connection()
        connections.append(rpc)
        return rpc

    async def run(monitor):
        await monitor.scan()
        if len(connections) == 2:
            raise asyncio.CancelledError()
        monitor.api.rpc.closed.set()

    monkeypatch.setattr(daemon, "Rpc", factory)
    monkeypatch.setattr(daemon.Monitor, "run", run)
    with pytest.raises(asyncio.CancelledError):
        await daemon.serve(settings, state)
    assert len(connections) == 2
    assert all(sum(m == "thread/list" for m, _ in rpc.calls) == 4 for rpc in connections)


async def test_failed_acquisition_never_generates(settings, state):
    rpc = Fake()
    rpc.fail_page = True
    monitor = Monitor(Api(rpc, settings), state, settings)
    with pytest.raises(RpcError):
        await monitor.repair("source")
    assert rpc.helper_count == 0
    assert not await state.allowed("source", rpc.name)
    await asyncio.sleep(0.02)
    assert await state.allowed("source", rpc.name)


async def test_duplicate_items_keep_distinct_versions(settings):
    rpc = Fake()
    original = rpc.turns[0]["items"][0]
    rpc.turns[0]["items"].extend([original, {**original, "newField": True}])
    snap = await Api(rpc, settings).snapshot("source")
    assert len(json.loads(snap.raw)["turns"][0]["items"]) == 3


async def test_model_missing_and_queue_bound(settings, state):
    rpc = Fake()

    async def models(method, params):
        return {"data": [], "nextCursor": None}

    rpc.call = models
    with pytest.raises(ValueError):
        await Generator(Api(rpc, settings), state, settings, set()).verify()
    settings.queue_size = 1
    monitor = Monitor(Api(rpc, settings), state, settings)
    monitor.schedule("first")
    monitor.schedule("overflow")
    assert list(monitor.pending) == ["first"]
    assert not monitor.eligible(Thread(id="missing", preview="x" * 500))


async def test_reroute_and_partial_output_never_applied(settings, state):
    rpc = Fake()
    original_call = rpc.call

    async def call(method, params):
        result = await original_call(method, params)
        if method == "turn/start":
            rpc.emit(
                "model/rerouted",
                threadId=params["threadId"],
                turnId="gen",
                fromModel="gpt-6-luna",
                toModel="other",
                reason="service",
            )
        return result

    rpc.call = call
    monitor = Monitor(Api(rpc, settings), state, settings)
    with pytest.raises(RuntimeError):
        await monitor.repair("source")
    assert not any(method == "thread/name/set" for method, _ in rpc.calls)
    assert any(method == "thread/unsubscribe" for method, _ in rpc.calls)


async def test_timeout_cleans_only_known_helper(settings, state):
    rpc = Fake()
    original_call = rpc.call

    async def call(method, params):
        if method == "turn/start":
            rpc.emit(
                "turn/started",
                threadId=params["threadId"],
                turn={"id": "known-active", "status": "inProgress"},
            )
            await asyncio.Future()
        return await original_call(method, params)

    rpc.call = call
    settings.generation_timeout = 0.01
    gen = Generator(Api(rpc, settings), state, settings, set())
    with pytest.raises(TimeoutError):
        await gen.generate(await gen.api.snapshot("source"), rpc.name)
    cleanup = [(m, p) for m, p in rpc.calls if m in ("turn/interrupt", "thread/unsubscribe")]
    assert len(cleanup) == 2
    assert all(params["threadId"] == "h1" for _, params in cleanup)
    assert rpc.helper_count == 1


async def test_uncertain_creation_not_replayed(settings, state):
    rpc = Fake()
    original_call = rpc.call

    async def call(method, params):
        if method == "thread/start":
            rpc.helper_count += 1
            raise TimeoutError()
        return await original_call(method, params)

    rpc.call = call
    gen = Generator(Api(rpc, settings), state, settings, set())
    with pytest.raises(TimeoutError):
        await gen.generate(await gen.api.snapshot("source"), rpc.name)
    assert rpc.helper_count == 1
