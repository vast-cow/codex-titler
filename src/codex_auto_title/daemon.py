import asyncio
import logging
import math
import random
import time
from dataclasses import dataclass

from .generation import Generator
from .history import Deferred, Snapshot, capture, pages
from .rpc import RPC, connection

log = logging.getLogger(__name__)


@dataclass
class Candidate:
    snapshot: Snapshot | None = None
    title: str | None = None
    done: bool = False
    retry_at: float = 0


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

    def event(self, method, params):
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
            if candidate and candidate.title == params["threadName"]:
                # Our own broadcast can precede the name-set acknowledgement.
                # Keep the worker alive to read back the result.
                return
            if candidate:
                candidate.done = True
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
        try:
            t = await self.metadata(tid)
            if t.get("ephemeral") is not False or t["name"] is not None:
                candidate.done = True
                return
            if candidate.snapshot is None:
                snap = await capture(self.rpc, tid, self.s.max_pages)
                if not any(snap.user_messages):
                    raise Deferred("empty thread; waiting for meaningful user input")
                if not snap.turns:
                    raise Deferred("waiting for first completed turn")
                candidate.snapshot = snap
                log.info(
                    "snapshot thread=%s hash=%s turns=%d interval=%.3f..%.3f",
                    tid,
                    snap.sha256,
                    len(snap.turns),
                    snap.started,
                    snap.finished,
                )
            if candidate.title is None:
                candidate.title = await self.generator.generate(candidate.snapshot)
            t = await self.metadata(tid)
            if candidate.done or t["name"] is not None:
                candidate.done = True
                return
            # Preserve title BEFORE sending. A disconnect/timeout triggers a reread on the
            # next pass; never automatically replay a mutation inside the RPC client.
            await self.rpc.request(
                "thread/name/set", {"threadId": tid, "name": candidate.title}
            )
            confirmed = await self.metadata(tid)
            if confirmed["name"] is None:
                raise Deferred(
                    "name-set acknowledged but readback is null; retain rename intent"
                )
            candidate.done = True
            log.info(
                "name readback thread=%s matches=%s",
                tid,
                confirmed["name"] == candidate.title,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            candidate.retry_at = time.monotonic() + max(30, self.s.poll_seconds)
            log.warning("deferred thread=%s: %s", tid, exc)

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
                try:
                    await asyncio.wait_for(rpc.closed.wait(), self.s.poll_seconds)
                except TimeoutError:
                    pass
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
