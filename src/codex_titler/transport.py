"""One reader per connection. Mutating requests are never automatically replayed."""

import asyncio
import contextlib
import json

import structlog
from websockets.asyncio.client import connect, unix_connect
from websockets.exceptions import ConnectionClosed

log = structlog.get_logger()


class RpcError(Exception):
    def __init__(self, error):
        super().__init__(str(error))
        self.error = error


class Rpc:
    def __init__(self, settings):
        self.s = settings
        self.pending = {}
        self.listeners = set()
        self.sequence = 0
        self.ws = self.process = self.reader = None
        self.closed = asyncio.Event()
        self.write_lock = asyncio.Lock()
        self.server_info = None

    async def open(self):
        if self.s.stdio_command:
            self.process = await asyncio.create_subprocess_exec(
                *self.s.stdio_command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                limit=self.s.max_message_bytes,
            )
        else:
            headers = (
                {"Authorization": "Bearer " + self.s.bearer_token.get_secret_value()}
                if self.s.bearer_token
                else None
            )
            kwargs = dict(max_size=self.s.max_message_bytes, additional_headers=headers)
            if self.s.endpoint.startswith("unix://"):
                self.ws = await unix_connect(self.s.endpoint[7:], **kwargs)
            else:
                self.ws = await connect(self.s.endpoint, **kwargs)
        self.reader = asyncio.create_task(self.receive())
        self.server_info = await self.call(
            "initialize",
            {
                "clientInfo": {"name": "codex_titler", "version": "0.1.0"},
                "capabilities": {"experimentalApi": True},
            },
        )
        await self.send({"method": "initialized", "params": {}})

    async def send(self, message):
        raw = json.dumps(message)
        async with self.write_lock:
            if self.ws:
                try:
                    await self.ws.send(raw)
                except ConnectionClosed as exc:
                    raise ConnectionError("WebSocket closed while sending") from exc
            else:
                self.process.stdin.write((raw + "\n").encode())
                await self.process.stdin.drain()

    async def call(self, method, params):
        if self.closed.is_set():
            raise ConnectionError("connection closed")
        self.sequence += 1
        key = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[key] = future
        try:
            await self.send({"id": key, "method": method, "params": params})
            return await asyncio.wait_for(future, self.s.request_timeout)
        finally:
            self.pending.pop(key, None)

    async def receive(self):
        try:
            while True:
                raw = await self.ws.recv() if self.ws else await self.process.stdout.readline()
                if not raw:
                    raise ConnectionError("EOF")
                msg = json.loads(raw)
                if "method" in msg:
                    if "id" in msg:
                        # Never authorize server-initiated tool actions or authentication changes.
                        await self.send(
                            {
                                "id": msg["id"],
                                "error": {
                                    "code": -32601,
                                    "message": "Title client does not handle server requests",
                                },
                            }
                        )
                    else:
                        params = msg.get("params", {})
                        if not isinstance(params, dict):
                            log.warning("malformed_notification", method=msg["method"])
                            continue
                        for callback in tuple(self.listeners):
                            try:
                                callback(msg["method"], params)
                            except (KeyError, TypeError, ValueError, AttributeError):
                                log.warning("malformed_notification", method=msg["method"])
                elif (future := self.pending.get(msg.get("id"))) and not future.done():
                    if "error" in msg:
                        future.set_exception(RpcError(msg["error"]))
                    else:
                        future.set_result(msg["result"])
        except Exception as exc:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ConnectionError(str(exc)))
        finally:
            self.closed.set()

    async def close(self):
        if self.reader:
            self.reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.reader
        if self.ws:
            await self.ws.close()
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
