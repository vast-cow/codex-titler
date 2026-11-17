import asyncio
import logging
import math
import random
import time
from dataclasses import dataclass, field

from .generation import Generator
from .history import Deferred, Snapshot, SourceRunning, Waiting, capture, pages
from .lifecycle import AttemptState, Exhausted
from .rpc import RPC, RpcError, connection

log = logging.getLogger(__name__)


@dataclass
class Candidate:
    snapshot: Snapshot | None = None
    title: str | None = None
    done: bool = False
    capture_deadline: float | None = None
    retry_at: float = 0
    attempts: AttemptState = field(default_factory=AttemptState)
    failures: int = 0
    writes: int = 0
    rename_uncertain: bool = False
    phase: str = "waiting"
    reason: str | None = None
    snapshot_sha256: str | None = None

    def finish(self, phase, reason=None):
        self.done = True
        self.phase, self.reason = phase, reason[:1000] if reason else None
        self.snapshot = None
        self.rename_uncertain = False


class Monitor:
    def __init__(self, settings):
        self.s = settings
        self.since = math.ceil(time.time())
        self.known = set()
        self.candidates = {}
        self.jobs = {}
        self.baselined = False
        self.rpc = None
        self.generator = None
        self.wake = asyncio.Event()

    def event(self, method, params):
        self.wake.set()
        if method == "thread/started":
            t = params["thread"]
            tid = t["id"]
            new = tid not in self.known
            self.known.add(tid)
            if (
                new
                and t.get("ephemeral") is False
                and type(t.get("createdAt")) is int
                and t["createdAt"] >= self.since
                and not t.get("forkedFromId")
                and not t.get("parentThreadId")
            ):
                self.candidates[tid] = Candidate()
        elif method == "thread/name/updated" and params.get("threadName") is not None:
            tid = params["threadId"]
            candidate = self.candidates.get(tid)
            if candidate and (
                candidate.title == params["threadName"] or candidate.rename_uncertain
            ):
                # Broadcasts can precede acknowledgement. Resolve uncertain writes
                # by readback, including notifications of a competing manual name.
                return
            if candidate:
                candidate.finish("named_elsewhere")
            job = self.jobs.get(tid)
            if job:
                job.cancel()
        # Source completion events may be subscriber-only. Read-only polling of registered
        # candidates restores completion/name state without resuming source threads.

    async def metadata(self, tid):
        response = await self.rpc.request(
            "thread/read", {"threadId": tid, "includeTurns": False}
        )
        t = response["thread"]
        if "name" not in t:
            raise Deferred("thread/read omitted name; cannot establish explicit null")
        return t

    async def process(self, tid):
        candidate = self.candidates[tid]
        if candidate.done:
            return
        cancelled = False
        try:
            t = await self.metadata(tid)
            if t.get("ephemeral") is not False or t["name"] is not None:
                candidate.finish(
                    "named"
                    if candidate.title is not None and candidate.title == t["name"]
                    else "ineligible"
                )
                return
            # A successful read of explicit null resolves any previous uncertain write.
            candidate.rename_uncertain = False
            if candidate.title is None:
                candidate.attempts.check(self.s)
                if candidate.snapshot is None:
                    if (
                        candidate.capture_deadline is not None
                        and time.monotonic() < candidate.capture_deadline
                    ):
                        raise SourceRunning("waiting for five-second capture deadline")
                    candidate.phase = "capturing"
                    snap = await capture(
                        self.rpc,
                        tid,
                        self.s.max_pages,
                        allow_running=candidate.capture_deadline is not None,
                    )
                    ready = await asyncio.to_thread(
                        lambda: bool(snap.boundaries) and any(snap.user_messages)
                    )
                    if not ready:
                        raise Waiting("waiting for meaningful user input")
                    candidate.snapshot = snap
                    candidate.snapshot_sha256 = snap.sha256
                    log.info("snapshot thread=%s hash=%s", tid, snap.sha256)
                candidate.phase = "generating"
                candidate.title = await self.generator.generate(
                    candidate.snapshot, candidate.attempts
                )
                # No further history is needed, even if the eventual write is ambiguous.
                candidate.snapshot = None
            t = await self.metadata(tid)
            if candidate.done or t["name"] is not None:
                candidate.finish("named_elsewhere")
                return
            if candidate.writes >= self.s.transient_failures:
                raise Exhausted(
                    "candidate name-write attempts exhausted after null readback"
                )
            candidate.writes += 1
            candidate.phase = "reconciling"
            candidate.rename_uncertain = True
            # Save intent before sending; timeouts include blocked sends and remain uncertain.
            await self.rpc.request(
                "thread/name/set", {"threadId": tid, "name": candidate.title}
            )
            confirmed = await self.metadata(tid)
            if confirmed["name"] is None:
                candidate.rename_uncertain = False
                raise RpcError("name-set acknowledged but readback is null")
            candidate.finish(
                "named" if confirmed["name"] == candidate.title else "named_elsewhere"
            )
            log.info(
                "name readback thread=%s matches=%s",
                tid,
                confirmed["name"] == candidate.title,
            )
        except asyncio.CancelledError:
            cancelled = True
            if candidate.done:
                candidate.snapshot = None
            raise
        except SourceRunning as exc:
            if candidate.capture_deadline is None:
                candidate.capture_deadline = time.monotonic() + 5
            candidate.phase, candidate.reason = "source_delay", str(exc)[:1000]
        except Waiting as exc:
            candidate.phase, candidate.reason = "waiting", str(exc)[:1000]
        except Exhausted as exc:
            candidate.finish("exhausted", str(exc))
        except (Deferred, ValueError) as exc:
            if candidate.rename_uncertain:
                candidate.phase, candidate.reason = "reconciling", str(exc)[:1000]
            else:
                candidate.finish("unsupported", str(exc))
        except Exception as exc:
            candidate.failures += 1
            candidate.reason = str(exc)[:1000]
            # An unresolved write is reconciled with reads only. Keep compact intent,
            # even when transport failure budgets are exhausted; never regenerate.
            if candidate.rename_uncertain:
                candidate.phase = "reconciling"
            elif candidate.failures >= self.s.transient_failures:
                candidate.finish("exhausted", str(exc))
            elif (
                isinstance(exc, RpcError)
                and isinstance(exc.payload, dict)
                and exc.payload.get("code") in (-32600, -32601, -32602)
            ):
                candidate.finish("unsupported", str(exc))
            else:
                candidate.phase = "transient"
                if candidate.title is None:
                    try:
                        candidate.attempts.check(self.s)
                    except Exhausted as exhausted:
                        candidate.finish("exhausted", str(exhausted))
        finally:
            if candidate.done:
                log.info(
                    "terminal thread=%s phase=%s reason=%s",
                    tid,
                    candidate.phase,
                    candidate.reason,
                )
            else:
                if not cancelled:
                    candidate.retry_at = (
                        candidate.capture_deadline
                        if candidate.phase == "source_delay"
                        else time.monotonic() + max(30, self.s.poll_seconds)
                    )
                log.warning(
                    "deferred thread=%s phase=%s: %s",
                    tid,
                    candidate.phase,
                    candidate.reason,
                )

    async def session(self, rpc):
        self.rpc = rpc
        self.generator = Generator(rpc, self.s)
        rpc.listeners.add(self.event)
        try:
            info = await rpc.initialize()
            log.info(
                "connected server=%s; monitoring new non-ephemeral thread/started events",
                info,
            )
            if not self.baselined:
                async for page in pages(rpc, "thread/list", {}, self.s.max_pages):
                    self.known.update(
                        t["id"] for t in page["data"] if t["id"] not in self.candidates
                    )
                self.baselined = True
            while not rpc.closed.is_set():
                self.wake.clear()
                for tid, job in tuple(self.jobs.items()):
                    if job.done():
                        # Retrieve unexpected task failures, including cancellation.
                        if not job.cancelled():
                            job.result()
                        del self.jobs[tid]
                for tid, candidate in self.candidates.items():
                    if len(self.jobs) >= self.s.workers:
                        break
                    if (
                        not candidate.done
                        and tid not in self.jobs
                        and candidate.retry_at <= time.monotonic()
                    ):
                        self.jobs[tid] = asyncio.create_task(self.process(tid))
                        self.jobs[tid].add_done_callback(lambda _: self.wake.set())
                # Free workers promptly and wake for candidate deadlines, independently
                # of the ordinary polling interval. Never overlap jobs for a thread.
                timeout = self.s.poll_seconds
                if len(self.jobs) < self.s.workers:
                    due = [
                        c.retry_at
                        for tid, c in self.candidates.items()
                        if not c.done and tid not in self.jobs
                    ]
                    if due:
                        timeout = min(timeout, max(0, min(due) - time.monotonic()))
                waits = [
                    asyncio.create_task(rpc.closed.wait()),
                    asyncio.create_task(self.wake.wait()),
                ]
                try:
                    await asyncio.wait(
                        waits, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
                    )
                finally:
                    for task in waits:
                        task.cancel()
                    await asyncio.gather(*waits, return_exceptions=True)
        finally:
            rpc.listeners.discard(self.event)
            for task in self.jobs.values():
                task.cancel()
            await asyncio.gather(*self.jobs.values(), return_exceptions=True)
            self.jobs.clear()

    async def run(self):
        delay = 1.0
        while True:
            try:
                async with (
                    connection(self.s) as ws,
                    RPC(ws, self.s.request_timeout) as rpc,
                ):
                    await self.session(rpc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("connection unavailable: %s", exc)
            await asyncio.sleep(delay + random.uniform(0, delay / 4))
            delay = min(delay * 2, self.s.reconnect_max)
