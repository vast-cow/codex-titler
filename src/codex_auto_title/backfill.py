"""Finite, opt-in historical scan using the monitor's candidate workflow."""

import asyncio
import json
import logging
import time
from collections import Counter

from .daemon import Candidate, Monitor
from .generation import Generator
from .history import pages
from .rpc import RPC, connection

log = logging.getLogger(__name__)


class Backfill(Monitor):
    def __init__(self, settings):
        super().__init__(settings)
        self.eligible = set()
        self.enumeration_complete = False
        self.error = None
        self.created_at = {}

    def event(self, method, params):
        # Only the scan enrolls candidates; helper/new-source broadcasts cannot.
        if method != "thread/started":
            super().event(method, params)

    async def metadata(self, tid):
        t = await super().metadata(tid)
        if (
            t.get("ephemeral") is False
            and not t.get("forkedFromId")
            and not t.get("parentThreadId")
            and t["name"] is None
        ):
            self.eligible.add(tid)
        return t

    async def scan(self):
        try:
            async for page in pages(
                self.rpc, "thread/list", {"archived": False}, self.s.max_pages
            ):
                for t in page["data"]:
                    tid = t["id"]
                    if tid in self.candidates:
                        continue
                    c = self.candidates[tid] = Candidate()
                    # Keep first-seen metadata, matching candidate deduplication.
                    created = t.get("createdAt")
                    self.created_at[tid] = created if type(created) is int else None
                    if (
                        t.get("ephemeral") is not False
                        or t.get("forkedFromId")
                        or t.get("parentThreadId")
                    ):
                        c.finish(
                            "ineligible", "ephemeral, fork, child, or unknown type"
                        )
            self.enumeration_complete = True
        except Exception as exc:
            self.error = f"incomplete enumeration: {exc}"
            # Do not mutate a partially enumerated population.
            for c in self.candidates.values():
                if not c.done:
                    c.finish("deferred", "enumeration incomplete; no titles written")

    def order_key(self, tid):
        created = self.created_at[tid]
        # Unknown times always follow known times; IDs break ties in both modes.
        timestamp = created if created is not None else 0
        if self.s.backfill_order == "newest-first":
            timestamp = -timestamp
        return (created is None, timestamp, tid)

    async def worker(self, queue):
        while not queue.empty():
            tid = queue.get_nowait()
            c = self.candidates[tid]
            reconciliation = 0
            while not c.done:
                job = self.jobs[tid] = asyncio.create_task(self.process(tid))
                try:
                    await job
                except asyncio.CancelledError:
                    # A manual rename cancels only this candidate's job. Batch
                    # shutdown must still propagate through the worker.
                    if asyncio.current_task().cancelling() or not c.done:
                        raise
                finally:
                    del self.jobs[tid]
                if c.done:
                    break
                if c.rename_uncertain:
                    reconciliation += 1
                    if reconciliation >= self.s.transient_failures:
                        c.finish("unresolved", c.reason or "name readback unavailable")
                elif c.phase == "waiting":
                    c.finish("deferred", c.reason)
                elif c.phase == "source_delay":
                    await asyncio.sleep(max(0, c.capture_deadline - time.monotonic()))
                # Other retries are bounded by the existing candidate budgets.
                # No polling delay is needed for a finite batch.
            queue.task_done()

    async def session(self, rpc):
        self.rpc = rpc
        self.generator = Generator(rpc, self.s)
        rpc.listeners.add(self.event)
        try:
            await self.scan()
            queue = asyncio.Queue()
            # Sort globally only after every page has been enumerated. Do not
            # depend on the server's list order or sort individual pages.
            pending = (tid for tid, c in self.candidates.items() if not c.done)
            for tid in sorted(pending, key=self.order_key):
                queue.put_nowait(tid)
            async with asyncio.TaskGroup() as group:
                for _ in range(self.s.workers):
                    group.create_task(self.worker(queue))
        finally:
            rpc.listeners.discard(self.event)

    def report(self):
        outcomes = Counter()
        mapping = {
            "named": "renamed",
            "ineligible": "skipped",
            "named_elsewhere": "skipped",
            "unsupported": "deferred",
            "exhausted": "failed",
            "deferred": "deferred",
            "unresolved": "unresolved",
        }
        for tid, c in self.candidates.items():
            outcome = mapping.get(c.phase, "unresolved")
            outcomes[outcome] += 1
            if outcome != "renamed":
                reason = c.reason or {
                    "ineligible": "current name is non-null or thread type is excluded",
                    "named_elsewhere": "name changed by another client",
                }.get(c.phase, c.phase)
                print(f"{outcome} thread={tid}: {reason}")
        summary = {
            "scanned": len(self.candidates),
            "eligible": len(self.eligible),
            **{
                k: outcomes[k]
                for k in ("renamed", "skipped", "deferred", "failed", "unresolved")
            },
            "enumeration_complete": self.enumeration_complete,
        }
        if self.error:
            print(self.error)
        print(json.dumps(summary, sort_keys=True))
        return int(
            bool(
                not self.enumeration_complete
                or self.error
                or outcomes["deferred"]
                or outcomes["failed"]
                or outcomes["unresolved"]
            )
        )

    async def run(self):
        # Only connection/initialization is retried. Once scanning starts, the
        # population is never scanned again, even if the connection is lost.
        for attempt in range(self.s.transient_failures):
            initialized = False
            try:
                async with (
                    connection(self.s) as ws,
                    RPC(ws, self.s.request_timeout) as rpc,
                ):
                    await rpc.initialize()
                    initialized = True
                    self.error = None
                    await self.session(rpc)
                break
            except Exception as exc:
                self.error = f"backfill connection/session failed: {exc}"
                log.warning("%s", self.error)
                if initialized:
                    for c in self.candidates.values():
                        if not c.done:
                            c.finish(
                                "unresolved" if c.rename_uncertain else "deferred",
                                self.error,
                            )
                    break
                if attempt + 1 < self.s.transient_failures:
                    await asyncio.sleep(min(2**attempt, self.s.reconnect_max))
        return self.report()
