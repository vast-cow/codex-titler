import hashlib
import time
import uuid

import aiosqlite


def title_hash(title):
    return hashlib.sha256(title.encode()).hexdigest()


class State:
    async def open(self, path):
        self.db = await aiosqlite.connect(path)
        self.db.row_factory = aiosqlite.Row
        await self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, original_hash TEXT NOT NULL,
                snapshot_hash TEXT, candidate TEXT, state TEXT NOT NULL, attempts INTEGER DEFAULT 0,
                next_retry REAL DEFAULT 0, outcome TEXT, started REAL NOT NULL, updated REAL NOT NULL,
                acquisition_start REAL, acquisition_end REAL);
            CREATE INDEX IF NOT EXISTS jobs_source ON jobs(thread_id, original_hash, updated);
            CREATE TABLE IF NOT EXISTS helpers (id TEXT PRIMARY KEY);
        """)
        await self.db.commit()
        # Interrupted jobs must be revalidated by metadata scans, never replayed blindly.
        await self.db.execute(
            "UPDATE jobs SET state='interrupted', next_retry=0 "
            "WHERE state IN ('acquiring','generating','renaming')"
        )
        await self.db.commit()
        return self

    async def allowed(self, thread_id, title):
        async with self.db.execute(
            "SELECT next_retry FROM jobs WHERE thread_id=? AND original_hash=? "
            "ORDER BY updated DESC LIMIT 1",
            (thread_id, title_hash(title)),
        ) as cursor:
            row = await cursor.fetchone()
        return not row or row[0] <= time.time()

    async def begin(self, thread_id, title):
        key, now = uuid.uuid4().hex, time.time()
        await self.db.execute(
            "INSERT INTO jobs(id,thread_id,original_hash,state,started,updated) "
            "VALUES(?,?,?,'acquiring',?,?)",
            (key, thread_id, title_hash(title), now, now),
        )
        await self.db.commit()
        return key

    async def update(self, key, **fields):
        fields["updated"] = time.time()
        await self.db.execute(
            "UPDATE jobs SET " + ",".join(f"{k}=?" for k in fields) + " WHERE id=?",
            (*fields.values(), key),
        )
        await self.db.commit()

    async def helper(self, thread_id):
        await self.db.execute("INSERT OR IGNORE INTO helpers VALUES(?)", (thread_id,))
        await self.db.commit()

    async def helpers(self):
        async with self.db.execute("SELECT id FROM helpers") as cursor:
            return {row[0] for row in await cursor.fetchall()}

    async def close(self):
        await self.db.close()
