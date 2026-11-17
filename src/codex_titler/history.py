import hashlib
import json
import time
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

SOURCES = [
    "cli",
    "vscode",
    "exec",
    "appServer",
    "subAgent",
    "subAgentReview",
    "subAgentCompact",
    "subAgentThreadSpawn",
    "subAgentOther",
    "unknown",
]


class Thread(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    id: str
    name: str | None = None
    ephemeral: bool = False


def serialize(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class Snapshot:
    # Canonical JSON is deeply immutable; callers get copies on decoding.
    raw: str
    started: float
    completed: float

    @property
    def digest(self):
        return hashlib.sha256(self.raw.encode()).hexdigest()


class Api:
    def __init__(self, rpc, settings):
        self.rpc, self.s = rpc, settings

    async def read(self, method, params):
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self.s.read_retries),
            wait=wait_exponential(multiplier=self.s.backoff, max=10),
            retry=retry_if_exception_type((TimeoutError, ConnectionError)),
            reraise=True,
        ):
            with attempt:
                return await self.rpc.call(method, params)

    async def pages(self, method, params):
        cursor, seen = None, set()
        while True:
            page = await self.read(method, {**params, "cursor": cursor, "limit": 100})
            if not isinstance(page.get("data"), list) or "nextCursor" not in page:
                raise ValueError(f"malformed {method} page")
            yield page
            cursor = page["nextCursor"]
            if cursor is None:
                break
            marker = serialize(cursor)
            if marker in seen:
                raise ValueError(f"repeated {method} cursor")
            seen.add(marker)

    async def threads(self):
        for archived in (False, True):
            async for page in self.pages(
                "thread/list",
                {
                    "archived": archived,
                    "sourceKinds": SOURCES,
                    "modelProviders": [],
                },
            ):
                for row in page["data"]:
                    yield Thread.model_validate(row)

    async def thread(self, thread_id):
        result = await self.read("thread/read", {"threadId": thread_id})
        return Thread.model_validate(result["thread"])

    async def snapshot(self, thread_id):
        started = time.time()
        turns, boundaries, seen = [], [], set()
        seen_items = set()
        async for page in self.pages(
            "thread/turns/list",
            {
                "threadId": thread_id,
                "sortDirection": "asc",
                "itemsView": "full",
            },
        ):
            boundaries.append({k: v for k, v in page.items() if k != "data"})
            for turn in page["data"]:
                if not isinstance(turn.get("id"), str) or not isinstance(turn.get("items"), list):
                    raise ValueError("malformed full turn")
                if turn.get("itemsView", "full") != "full":
                    raise ValueError("server did not return full items")
                # Deduplicate identical records only. Changed records with the same ID survive.
                record = serialize(turn)
                if record not in seen:
                    seen.add(record)
                    unique_items = []
                    for item in turn["items"]:
                        item_key = (turn["id"], serialize(item))
                        if item_key not in seen_items:
                            seen_items.add(item_key)
                            unique_items.append(item)
                    turns.append({**turn, "items": unique_items})
        # Full turn view includes full items, without output deferral. Preserve unknown fields.
        raw = serialize({"threadId": thread_id, "turns": turns, "boundaries": boundaries})
        return Snapshot(raw, started, time.time())
