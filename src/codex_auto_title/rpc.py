"""One reader owns the WebSocket; workers never receive from it directly."""

import asyncio
import json
import logging
import socket
from contextlib import asynccontextmanager, suppress
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict
from websockets.asyncio.client import connect, unix_connect

log = logging.getLogger(__name__)


class RpcError(Exception):
    def __init__(self, error):
        self.payload = error
        super().__init__(json.dumps(error) if isinstance(error, dict) else error)
        data = error.get("data") if isinstance(error, dict) else None
        self.context_size = (
            isinstance(data, dict)
            and data.get("codexErrorInfo") == "contextWindowExceeded"
        )


class Disconnected(RpcError):
    pass


class Envelope(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: int | str | None = None
    method: str | None = None
    params: dict | None = None


@asynccontextmanager
async def connection(settings):
    options = dict(
        additional_headers=settings.headers(),
        max_size=settings.max_message_bytes,
        open_timeout=settings.request_timeout,
        close_timeout=5,
    )
    try:
        if urlsplit(settings.endpoint).scheme == "unix":
            if not hasattr(socket, "AF_UNIX"):
                raise RpcError(
                    "AF_UNIX is unavailable on this platform; use ws:// or wss://"
                )
            client = unix_connect(
                settings.socket_path, uri=settings.unix_uri, **options
            )
        else:
            # websockets uses a verifying default SSL context for wss://.
            client = connect(settings.endpoint, **options)
        async with client as ws:
            yield ws
    except FileNotFoundError as exc:
        raise RpcError(
            "Unix socket missing: check --endpoint and the existing server listener"
        ) from exc
    except PermissionError as exc:
        raise RpcError(
            "Permission denied: check socket and parent-directory access"
        ) from exc
    except ConnectionRefusedError as exc:
        raise RpcError(
            "Connection refused: verify the existing server is listening at --endpoint"
        ) from exc


class RPC:
    def __init__(self, ws, timeout=30):
        self.ws, self.timeout = ws, timeout
        self.pending = {}
        self.sequence = 0
        self.listeners = set()
        self.reader = None
        self.closed = asyncio.Event()

    async def __aenter__(self):
        self.reader = asyncio.create_task(self.receive())
        return self

    async def __aexit__(self, *_):
        self.reader.cancel()
        with suppress(asyncio.CancelledError):
            await self.reader

    async def send(self, message):
        try:
            async with asyncio.timeout(self.timeout):
                await self.ws.send(json.dumps(message))
        except TimeoutError:
            raise
        except Exception as exc:
            raise Disconnected(
                "WebSocket send failed; mutation outcome may be unknown"
            ) from exc

    async def request(self, method, params=None):
        self.sequence += 1
        request_id = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            async with asyncio.timeout(self.timeout):
                await self.send(
                    {"id": request_id, "method": method, "params": params or {}}
                )
                return await future
        finally:
            self.pending.pop(request_id, None)
            if not future.done():
                future.cancel()

    async def initialize(self):
        result = await self.request(
            "initialize",
            {
                "clientInfo": {"name": "codex_auto_title", "version": "0.1.0"},
                "capabilities": {"experimentalApi": True},
            },
        )
        await self.send({"method": "initialized", "params": {}})
        return result

    async def receive(self):
        try:
            async for raw in self.ws:
                message = json.loads(raw)
                envelope = Envelope.model_validate(message)
                if envelope.method is not None:
                    if envelope.id is not None:
                        # Helpers must never acquire capabilities or execute requested tools.
                        await self.send(
                            {
                                "id": envelope.id,
                                "error": {
                                    "code": -32601,
                                    "message": "Auto-title client does not execute tools or approvals",
                                },
                            }
                        )
                    else:
                        for listener in tuple(self.listeners):
                            listener(envelope.method, envelope.params or {})
                elif envelope.id in self.pending:
                    future = self.pending[envelope.id]
                    if not future.done():
                        if "error" in message:
                            future.set_exception(RpcError(message["error"]))
                        else:
                            future.set_result(message["result"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("receive stopped: %s", exc)
        finally:
            self.closed.set()
            for future in tuple(self.pending.values()):
                if not future.done():
                    future.set_exception(Disconnected("connection closed"))
