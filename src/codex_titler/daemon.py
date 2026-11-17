import asyncio
import contextlib
import time

import structlog

from .generation import Generator
from .history import Api
from .titles import graphemes
from .transport import Rpc, RpcError

log = structlog.get_logger()


class Monitor:
    def __init__(self, api, state, settings):
        self.api, self.state, self.s = api, state, settings
        self.queue = asyncio.Queue(settings.queue_size)
        self.pending = {}  # Bounded by queue capacity plus active worker count.
        self.active = set()
        self.dirty = set()
        self.helpers = set()
        self.generator = Generator(api, state, settings, self.helpers)

    def eligible(self, thread):
        return (
            thread.id not in self.helpers
            and not thread.ephemeral
            and thread.name is not None
            and graphemes(thread.name) > self.s.threshold
        )

    def schedule(self, thread_id):
        if thread_id in self.helpers:
            return
        if thread_id in self.active:
            self.dirty.add(thread_id)
        elif thread_id in self.pending:
            self.pending[thread_id] = time.monotonic() + self.s.debounce
        else:
            try:
                self.queue.put_nowait(thread_id)
                self.pending[thread_id] = time.monotonic() + self.s.debounce
            except asyncio.QueueFull:
                log.warning("queue_full", thread_id=thread_id, recovery="next reconciliation")

    def notification(self, method, params):
        if method == "thread/name/updated" and isinstance(params.get("threadId"), str):
            self.schedule(params["threadId"])

    async def scan(self):
        started = time.monotonic()
        scanned = 0
        eligible = 0

        log.info(
            "scan_started",
            reconcile_interval=self.s.reconcile_interval,
        )

        async for thread in self.api.threads():
            scanned += 1
            if self.eligible(thread):
                eligible += 1
                self.schedule(thread.id)

        log.info(
            "scan_finished",
            scanned=scanned,
            eligible=eligible,
            pending=len(self.pending),
            active=len(self.active),
            duration_seconds=round(time.monotonic() - started, 3),
        )

    async def worker(self):
        while True:
            thread_id = await self.queue.get()
            try:
                while (delay := self.pending[thread_id] - time.monotonic()) > 0:
                    await asyncio.sleep(delay)
                self.pending.pop(thread_id)
                self.active.add(thread_id)
                await self.repair(thread_id)
            except Exception as exc:
                log.error("job_error", thread_id=thread_id, error_type=type(exc).__name__)
            finally:
                self.active.discard(thread_id)
                self.pending.pop(thread_id, None)
                self.queue.task_done()
                if thread_id in self.dirty:
                    self.dirty.discard(thread_id)
                    self.schedule(thread_id)

    async def repair(self, thread_id):
        thread = await self.api.thread(thread_id)
        if not self.eligible(thread) or not await self.state.allowed(thread_id, thread.name):
            return
        original = thread.name
        job = await self.state.begin(thread_id, original)
        try:
            snapshot = await self.api.snapshot(thread_id)
            await self.state.update(
                job,
                state="generating",
                snapshot_hash=snapshot.digest,
                acquisition_start=snapshot.started,
                acquisition_end=snapshot.completed,
                attempts=1,
            )
            candidate, truncated = await self.generator.generate(snapshot, original, job)
            await self.state.update(job, candidate=candidate)
            current = await self.api.thread(thread_id)
            if current.name != original or not self.eligible(current):
                outcome = "stale"
                self.dirty.add(thread_id)
            elif self.s.dry_run:
                outcome = "dry_run"
                log.info("candidate", job_id=job, thread_id=thread_id, candidate=candidate)
            else:
                await self.state.update(job, state="renaming")
                try:
                    await self.api.rpc.call(
                        "thread/name/set",
                        {
                            "threadId": thread_id,
                            "name": candidate,
                        },
                    )
                    outcome = "renamed"
                except (TimeoutError, ConnectionError):
                    # Never replay an uncertain write. A later scan revalidates if read fails.
                    current = await self.api.thread(thread_id)
                    outcome = "rename_confirmed" if current.name == candidate else "write_uncertain"
            await self.state.update(
                job, state="done", outcome=outcome, next_retry=time.time() + self.s.cooldown
            )
            log.info(
                "job_finished",
                job_id=job,
                thread_id=thread_id,
                outcome=outcome,
                snapshot_hash=snapshot.digest,
                truncated=truncated,
                acquisition_seconds=snapshot.completed - snapshot.started,
            )
        except BaseException as exc:
            await self.state.update(
                job,
                state="failed",
                outcome=type(exc).__name__,
                next_retry=time.time() + self.s.cooldown,
            )
            raise

    async def run(self):
        self.helpers.update(await self.state.helpers())
        await self.generator.verify()
        self.api.rpc.listeners.add(self.notification)
        workers = [asyncio.create_task(self.worker()) for _ in range(self.s.concurrency)]
        try:
            while not self.api.rpc.closed.is_set():
                try:
                    await self.scan()
                except (RpcError, ValueError) as exc:
                    log.error("scan_failed", error_type=type(exc).__name__)
                try:
                    await asyncio.wait_for(self.api.rpc.closed.wait(), self.s.reconcile_interval)
                except TimeoutError:
                    pass
        finally:
            self.api.rpc.listeners.discard(self.notification)
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)


async def serve(settings, state):
    while True:
        rpc = Rpc(settings)
        try:
            await rpc.open()
            log.info("connected", server=rpc.server_info)
            await Monitor(Api(rpc, settings), state, settings).run()
        except (ConnectionError, OSError, TimeoutError) as exc:
            log.warning("connection_lost", error_type=type(exc).__name__)
        finally:
            with contextlib.suppress(Exception):
                await rpc.close()
        await asyncio.sleep(settings.backoff)
