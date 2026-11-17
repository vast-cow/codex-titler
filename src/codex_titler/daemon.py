import asyncio
import contextlib
import time
from dataclasses import dataclass

import structlog

from .generation import Generator
from .history import Api
from .titles import graphemes
from .transport import Rpc, RpcError

log = structlog.get_logger()


@dataclass
class ScanResult:
    scanned: int = 0
    renamed: int = 0
    dry_run: int = 0
    skipped: int = 0
    failed: int = 0
    unresolved: int = 0
    errors: int = 0
    scan_complete: bool = False

    @property
    def exit_code(self):
        return int(not self.scan_complete or bool(self.failed or self.unresolved or self.errors))

    def log(self):
        log.info("scan_completed", **vars(self), exit_code=self.exit_code)


class Monitor:
    def __init__(self, api, state, settings, *, once=False, result=None):
        self.api, self.state, self.s = api, state, settings
        self.once = once
        self.result = result if result is not None else ScanResult()
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
        if self.once or thread_id in self.helpers:
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

        seen = set()
        async for thread in self.api.threads():
            if self.once:
                if thread.id in seen:
                    continue
                seen.add(thread.id)
                self.result.scanned += 1
            scanned += 1
            if self.eligible(thread):
                eligible += 1
                if self.once:
                    # A bounded producer: every discovered ID is admitted before moving on.
                    await self.queue.put(thread.id)
                    self.pending[thread.id] = time.monotonic() + self.s.debounce
                else:
                    self.schedule(thread.id)
            elif self.once:
                self.result.skipped += 1

        if self.once:
            self.result.scan_complete = True
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
                outcome = await self.repair(thread_id)
                if self.once:
                    if outcome in ("renamed", "rename_confirmed"):
                        self.result.renamed += 1
                    elif outcome == "dry_run":
                        self.result.dry_run += 1
                    elif outcome == "write_uncertain":
                        self.result.unresolved += 1
                    else:
                        self.result.skipped += 1
            except Exception as exc:
                if self.once:
                    self.result.failed += 1
                log.error("job_error", thread_id=thread_id, error_type=type(exc).__name__)
            finally:
                self.active.discard(thread_id)
                self.pending.pop(thread_id, None)
                self.queue.task_done()
                if not self.once and thread_id in self.dirty:
                    self.dirty.discard(thread_id)
                    self.schedule(thread_id)

    async def repair(self, thread_id):
        thread = await self.api.thread(thread_id)
        if not self.eligible(thread) or not await self.state.allowed(thread_id, thread.name):
            return "skipped"
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
                if not self.once:
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
            return outcome
        except BaseException as exc:
            await self.state.update(
                job,
                state="failed",
                outcome=type(exc).__name__,
                next_retry=time.time() + self.s.cooldown,
            )
            raise

    async def run_once(self):
        workers = []
        try:
            self.helpers.update(await self.state.helpers())
            await self.generator.verify()
            # Generation installs its own listeners; source-title events add no work here.
            workers = [asyncio.create_task(self.worker()) for _ in range(self.s.concurrency)]
            try:
                await self.scan()
            except Exception as exc:
                self.result.errors += 1
                log.error("scan_failed", error_type=type(exc).__name__)
            # Even a partial enumeration drains all work it already admitted.
            await self.queue.join()
            if self.api.rpc.closed.is_set():
                self.result.errors += 1
        finally:
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

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


async def scan_once(settings, state):
    result = ScanResult()
    rpc = Rpc(settings)
    try:
        await rpc.open()
        log.info("connected", server=rpc.server_info)
        await Monitor(Api(rpc, settings), state, settings, once=True, result=result).run_once()
    except asyncio.CancelledError:
        result.errors += 1
        raise
    except Exception as exc:
        result.errors += 1
        log.error("scan_error", error_type=type(exc).__name__)
    finally:
        try:
            await rpc.close()
        except Exception as exc:
            result.errors += 1
            log.error("close_error", error_type=type(exc).__name__)
        result.log()
    return result.exit_code
