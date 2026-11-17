import asyncio
import json
import time

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


async def test_failed_acquisition_never_generates(settings, state, monkeypatch):
    settings.cooldown = 300
    rpc = Fake()
    rpc.fail_page = True
    monitor = Monitor(Api(rpc, settings), state, settings)
    with pytest.raises(RpcError):
        await monitor.repair("source")
    assert rpc.helper_count == 0
    assert not await state.allowed("source", rpc.name)
    after_cooldown = time.time() + settings.cooldown + 1
    monkeypatch.setattr("codex_titler.state.time.time", lambda: after_cooldown)
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


async def test_once_backpressure_dedup_filter_and_drain(settings, state):
    settings.queue_size = 1
    settings.concurrency = 2
    rpc = Fake()
    api = Api(rpc, settings)
    rows = [Thread(id=str(i), name=rpc.name) for i in range(12)]
    rows += [
        rows[0],
        Thread(id="short", name="short"),
        Thread(id="missing"),
        Thread(id="ephemeral", name=rpc.name, ephemeral=True),
        Thread(id="own", name=rpc.name),
    ]
    await state.helper("own")

    async def threads():
        for row in rows:
            yield row

    api.threads = threads
    monitor = Monitor(api, state, settings, once=True)
    entered, release = asyncio.Event(), asyncio.Event()
    finished = []
    active = maximum = 0

    async def repair(thread_id):
        nonlocal active, maximum
        active += 1
        maximum = max(active, maximum)
        entered.set()
        await release.wait()
        # Neither notifications nor explicit scheduling can extend a one-shot run.
        monitor.notification("thread/name/updated", {"threadId": "extra"})
        monitor.schedule(thread_id)
        await asyncio.sleep(0)
        finished.append(thread_id)
        active -= 1
        return "renamed"

    monitor.repair = repair
    task = asyncio.create_task(monitor.run_once())
    await entered.wait()
    await asyncio.sleep(0.02)
    assert not task.done()
    assert monitor.queue.qsize() == 1
    release.set()
    await asyncio.wait_for(task, 2)
    assert set(finished) == {str(i) for i in range(12)}
    assert len(finished) == 12 and maximum == 2
    assert monitor.result.scanned == 16
    assert monitor.result.renamed == 12 and monitor.result.skipped == 4
    assert monitor.result.exit_code == 0
    assert not monitor.pending and not monitor.active and not monitor.dirty


@pytest.mark.parametrize(
    "mode,code",
    [
        ("normal", 0),
        ("dry", 0),
        ("stale", 0),
        ("cooldown", 0),
        ("empty", 0),
        ("short", 0),
        ("scan_failure", 1),
        ("worker_failure", 1),
        ("lost", 1),
        ("uncertain", 1),
        ("confirmed", 0),
        ("setup_failure", 1),
    ],
)
async def test_once_pipeline_exit_and_cleanup(settings, state, monkeypatch, mode, code):
    from codex_titler import daemon

    class Connection(Fake):
        server_info = {}
        closes = 0

        async def open(self):
            if mode == "setup_failure":
                raise ConnectionError("cannot connect")

        async def close(self):
            self.closes += 1
            self.closed.set()

        async def call(self, method, params):
            if method == "thread/list":
                if mode == "scan_failure" and params["cursor"]:
                    raise RpcError({"message": "page failed"})
                if mode == "empty":
                    self.calls.append((method, params))
                    return {"data": [], "nextCursor": None}
            if method == "thread/name/set" and mode == "uncertain":
                self.calls.append((method, params))
                raise TimeoutError()
            if method == "thread/turns/list" and mode == "lost":
                self.closed.set()
                raise ConnectionError("lost")
            return await super().call(method, params)

    rpc = Connection()
    rpc.fail_page = mode == "worker_failure"
    rpc.change_title = mode == "stale"
    rpc.rename_lost = mode == "confirmed"
    settings.dry_run = mode == "dry"
    if mode == "short":
        rpc.name = "short"
    if mode == "cooldown":
        job = await state.begin("source", rpc.name)
        await state.update(job, state="done", outcome="dry_run", next_retry=10**12)
    monkeypatch.setattr(daemon, "Rpc", lambda _: rpc)
    assert await asyncio.wait_for(daemon.scan_once(settings, state), 2) == code
    assert rpc.closes == 1 and not rpc.listeners
    if mode in ("dry", "stale", "empty", "short", "worker_failure", "lost", "cooldown"):
        assert not any(m == "thread/name/set" for m, _ in rpc.calls)
    if mode in ("normal", "dry", "stale", "uncertain", "confirmed"):
        async with state.db.execute("SELECT outcome FROM jobs") as cur:
            assert [row[0] for row in await cur.fetchall()] == [
                {
                    "normal": "renamed",
                    "dry": "dry_run",
                    "stale": "stale",
                    "uncertain": "write_uncertain",
                    "confirmed": "rename_confirmed",
                }[mode]
            ]
    if mode == "normal":
        calls = [p for m, p in rpc.calls if m == "thread/list"]
        assert [p["archived"] for p in calls] == [False, False, True, True]
        assert all(p["sourceKinds"] == SOURCES and p["modelProviders"] == [] for p in calls)
        assert rpc.helper_count == 1  # Duplicate IDs across archive pages are processed once.


@pytest.mark.parametrize(
    "args,once,dry",
    [
        ([], False, False),
        (["--dry-run"], False, True),
        (["scan", "--once"], True, False),
        (["scan", "--once", "--dry-run"], True, True),
        (["--dry-run", "scan", "--once"], True, True),
    ],
)
def test_cli_compatibility(monkeypatch, tmp_path, args, once, dry):
    import sys

    from codex_titler import cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TITLER_CONTEXT_LIMIT", "32000")
    monkeypatch.setenv("TITLER_DRY_RUN", "false")
    monkeypatch.setattr(sys, "argv", ["codex-titler", *args])

    async def run(settings, *, once=False):
        assert once == expected_once
        assert settings.dry_run == dry
        return 0

    expected_once = once
    monkeypatch.setattr(cli, "run", run)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0


async def test_once_stale_long_title_is_not_rescheduled(settings, state):
    rpc = Fake()
    original_call = rpc.call

    async def call(method, params):
        result = await original_call(method, params)
        if method == "turn/start":
            rpc.name = "Another eligible title " * 12
            rpc.emit("thread/name/updated", threadId="source")
        return result

    rpc.call = call
    monitor = Monitor(Api(rpc, settings), state, settings, once=True)
    await asyncio.wait_for(monitor.run_once(), 1)
    assert monitor.result.skipped == 2  # One stale source and one ephemeral helper.
    assert monitor.result.exit_code == 0
    assert rpc.helper_count == 1 and not monitor.dirty
    assert not any(m == "thread/name/set" for m, _ in rpc.calls)


async def test_once_interruption_closes_workers_transport_and_state(settings, monkeypatch):
    from codex_titler import cli, daemon

    entered = asyncio.Event()

    class Connection(Fake):
        server_info = {}
        closes = 0

        async def open(self):
            pass

        async def close(self):
            self.closes += 1
            self.closed.set()

        async def call(self, method, params):
            if method == "turn/start":
                entered.set()
                await asyncio.Future()
            return await super().call(method, params)

    rpc = Connection()
    state = State()
    monkeypatch.setattr(daemon, "Rpc", lambda _: rpc)
    monkeypatch.setattr(cli, "State", lambda: state)
    task = asyncio.create_task(cli.run(settings, once=True))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    assert await asyncio.wait_for(task, 1) == 130
    assert rpc.closes == 1 and not rpc.listeners
    assert any(m == "thread/unsubscribe" for m, _ in rpc.calls)
    with pytest.raises(ValueError, match="no active connection"):
        await state.db.execute("SELECT 1")


@pytest.mark.parametrize("code", [0, 1, 130])
def test_cli_exit_and_environment_dry_run(monkeypatch, tmp_path, code):
    import sys

    from codex_titler import cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TITLER_CONTEXT_LIMIT", "32000")
    monkeypatch.setenv("TITLER_DRY_RUN", "true")
    monkeypatch.setattr(sys, "argv", ["codex-titler", "scan", "--once"])

    async def run(settings, *, once=False):
        assert once and settings.dry_run
        return code

    monkeypatch.setattr(cli, "run", run)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == code
